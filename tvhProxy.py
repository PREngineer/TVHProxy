from gevent import monkey

monkey.patch_all()
import json
from ssdp import SSDPServer
from flask import Flask, jsonify, render_template, Response
from gevent.pywsgi import WSGIServer
import xml.etree.ElementTree as ElementTree
from datetime import timedelta, datetime, time
import logging
import socket
import threading
import time as time_mod
from requests.auth import HTTPDigestAuth
import requests
import os
import sched

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
scheduler = sched.scheduler()
logger = logging.getLogger()

host_name = socket.gethostname()
host_ip = socket.gethostbyname(host_name)

# URL format: <protocol>://<username>:<password>@<hostname>:<port>, example: https://test:1234@localhost:9981
config = {
    "deviceID": os.environ.get("DEVICE_ID") or "12345678",
    "bindAddr": "0.0.0.0",
    # only used if set (in case of forward-proxy)
    "tvhURL": os.environ.get("TVH_URL") or "http://localhost:9981",
    "tvhProxyURL": os.environ.get("TVH_PROXY_URL"),
    "tvhProxyHost": os.environ.get("TVH_PROXY_HOST") or host_ip,
    "tvhProxyPort": os.environ.get("TVH_PROXY_PORT") or 5004,
    "tvhUser": os.environ.get("TVH_USER") or "",
    "tvhPassword": os.environ.get("TVH_PASSWORD") or "",
    # number of tuners in tvh
    "tunerCount": os.environ.get("TVH_TUNER_COUNT") or 1,
    "tvhWeight": os.environ.get("TVH_WEIGHT") or 300,  # subscription priority
    # usually you don't need to edit this
    "chunkSize": os.environ.get("TVH_CHUNK_SIZE") or 1024 * 1024,
    # specifiy a stream profile that you want to use for adhoc transcoding in tvh, e.g. mp4
    "streamProfile": os.environ.get("TVH_PROFILE") or "pass",
}

# Local cache file for EPG XML
CACHE_FILE = os.environ.get("TVH_CACHE_FILE") or "epg.xml"
discoverData = {
    "FriendlyName": "tvhProxy",
    "Manufacturer": "Silicondust",
    "ModelNumber": "HDTC-2US",
    "FirmwareName": "hdhomeruntc_atsc",
    "TunerCount": int(config["tunerCount"]),
    "FirmwareVersion": "20150826",
    "DeviceID": config["deviceID"],
    "DeviceAuth": "test1234",
    "BaseURL": "%s" % (
        config["tvhProxyURL"]
        or "http://" + config["tvhProxyHost"] + ":" + str(config["tvhProxyPort"])
    ),
    "LineupURL": "%s/lineup.json" % (
        config["tvhProxyURL"]
        or "http://" + config["tvhProxyHost"] + ":" + str(config["tvhProxyPort"])
    ),
}

# EPG update schedule/time (HH:MM) and fetch lock (default midnight)
EPG_UPDATE_TIME = os.environ.get("TVH_EPG_UPDATE_TIME") or "00:00"
_fetch_lock = threading.Lock()


def _read_cache():
    try:
        with open(CACHE_FILE, "rb") as fh:
            return fh.read()
    except Exception:
        return None


def _write_cache(data_bytes):
    tmp = CACHE_FILE + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data_bytes)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        logger.error("Failed to write cache: %s", e)


def _get_genres():
    try:
        url = "%s/api/epg/content_type/list" % config["tvhURL"]
        params = {"full": 1}
        r = requests.get(url, auth=HTTPDigestAuth(config["tvhUser"], config["tvhPassword"]))
        entries = r.json(strict=False)["entries"]
        r = requests.get(url, params=params, auth=HTTPDigestAuth(config["tvhUser"], config["tvhPassword"]))
        entries_full = r.json(strict=False)["entries"]

        majorCategories = {}
        genres = {}
        for entry in entries:
            majorCategories[entry["key"]] = entry["val"]
        for entry in entries_full:
            if entry["key"] not in majorCategories:
                # find nearest previous major category
                prevKey = None
                for currentKey in sorted(majorCategories.keys()):
                    if currentKey > entry["key"]:
                        break
                    prevKey = currentKey
                mainCategory = majorCategories.get(prevKey, entry["val"])
                if mainCategory != entry["val"]:
                    genres[entry["key"]] = [mainCategory, entry["val"]]
                else:
                    genres[entry["key"]] = [entry["val"]]
            else:
                genres[entry["key"]] = [entry["val"]]
        return genres
    except Exception as e:
        logger.error("Failed to fetch genres: %s", e)
        return {}


def _build_xmltv():
    """Fetch XMLTV and EPG grid from TVHeadend and return serialized XML bytes."""
    url = "%s/xmltv/channels" % config["tvhURL"]
    r = requests.get(url, auth=HTTPDigestAuth(config["tvhUser"], config["tvhPassword"]))
    logger.info("downloading xmltv from %s", r.url)
    tree = ElementTree.ElementTree(ElementTree.fromstring(r.content))
    root = tree.getroot()

    url = "%s/api/epg/events/grid" % config["tvhURL"]
    params = {
        "limit": 999999,
        "filter": json.dumps(
            [
                {
                    "field": "start",
                    "type": "numeric",
                    "value": int(round(datetime.timestamp(datetime.now() + timedelta(hours=72)))),
                    "comparison": "lt",
                }
            ]
        ),
    }
    r = requests.get(url, params=params, auth=HTTPDigestAuth(config["tvhUser"], config["tvhPassword"]))
    logger.info("downloading epg grid from %s", r.url)
    epg_events_grid = r.json(strict=False)["entries"]
    epg_events = {}
    for epg_event in epg_events_grid:
        if epg_event["channelUuid"] not in epg_events:
            epg_events[epg_event["channelUuid"]] = {}
        epg_events[epg_event["channelUuid"]][epg_event["start"]] = epg_event

    channelNumberMapping = {}
    channelsInEPG = {}
    genres = _get_genres()
    for child in root:
        if child.tag == "channel":
            channelId = child.attrib["id"]
            channelNo = child[1].text
            if not channelNo:
                logger.error("No channel number for: %s", channelId)
                channelNo = "00"
            if not child[0].text:
                logger.error("No channel name for: %s", channelNo)
                child[0].text = "No Name"
            channelNumberMapping[channelId] = channelNo
            if channelNo in channelsInEPG:
                logger.error("duplicate channelNo: %s", channelNo)

            channelsInEPG[channelNo] = False
            for icon in child.iter("icon"):
                iconUrl = icon.attrib.get("src", "")
                try:
                    r = requests.head(iconUrl)
                    if r.status_code != requests.codes.ok:
                        logger.error("remove icon: %s", iconUrl)
                        child.remove(icon)
                except Exception:
                    child.remove(icon)

            child.attrib["id"] = channelNo
        if child.tag == "programme":
            channelUuid = child.attrib["channel"]
            channelNumber = channelNumberMapping[channelUuid]
            channelsInEPG[channelNumber] = True
            child.attrib["channel"] = channelNumber
            start_datetime = (
                datetime.strptime(child.attrib["start"], "%Y%m%d%H%M%S %z").astimezone(tz=None).replace(tzinfo=None)
            )
            stop_datetime = (
                datetime.strptime(child.attrib["stop"], "%Y%m%d%H%M%S %z").astimezone(tz=None).replace(tzinfo=None)
            )
            if stop_datetime > datetime.now() and start_datetime < datetime.now() + timedelta(hours=72):
                start_timestamp = int(round(datetime.timestamp(start_datetime)))
                epg_event = epg_events[channelUuid][start_timestamp]
                if "image" in epg_event:
                    programmeImage = ElementTree.SubElement(child, "icon")
                    imageUrl = str(epg_event["image"])
                    if imageUrl.startswith("imagecache"):
                        imageUrl = config["tvhURL"] + "/" + imageUrl + ".png"
                    programmeImage.attrib["src"] = imageUrl
                if "genre" in epg_event:
                    for genreId in epg_event["genre"]:
                        for category in genres[genreId]:
                            programmeCategory = ElementTree.SubElement(child, "category")
                            programmeCategory.text = category
                if "episodeOnscreen" in epg_event:
                    episodeNum = ElementTree.SubElement(child, "episode-num")
                    episodeNum.attrib["system"] = "onscreen"
                    episodeNum.text = epg_event["episodeOnscreen"]
                if "hd" in epg_event:
                    video = ElementTree.SubElement(child, "video")
                    quality = ElementTree.SubElement(video, "quality")
                    quality.text = "HDTV"
                if "new" in epg_event:
                    ElementTree.SubElement(child, "new")
                else:
                    ElementTree.SubElement(child, "previously-shown")
                if "copyright_year" in epg_event:
                    date = ElementTree.SubElement(child, "date")
                    date.text = str(epg_event["copyright_year"])
                del epg_events[channelUuid][start_timestamp]

    for key in sorted(channelsInEPG):
        if channelsInEPG[key]:
            logger.debug("Programmes found for channel %s", key)
        else:
            channelName = root.find('channel[@id="' + key + '"]/display-name').text
            logger.error("No programme for channel %s: %s", key, channelName)
            yesterday_midnight = datetime.combine(datetime.today(), time.min) - timedelta(days=1)
            date_format = "%Y%m%d%H%M%S"
            for x in range(0, 36):
                dummyProgramme = ElementTree.SubElement(root, "programme")
                dummyProgramme.attrib["channel"] = str(key)
                dummyProgramme.attrib["start"] = (yesterday_midnight + timedelta(hours=x * 2)).strftime(date_format)
                dummyProgramme.attrib["stop"] = (yesterday_midnight + timedelta(hours=(x * 2) + 2)).strftime(date_format)
                dummyTitle = ElementTree.SubElement(dummyProgramme, "title")
                dummyTitle.attrib["lang"] = "eng"
                dummyTitle.text = channelName
                dummyDesc = ElementTree.SubElement(dummyProgramme, "desc")
                dummyDesc.attrib["lang"] = "eng"
                dummyDesc.text = "No programming information"

    logger.info("built epg xml")
    return ElementTree.tostring(root)




def _fetch_and_update_cache():
    if _fetch_lock.locked():
        logger.debug("Fetch already in progress, skipping")
        return
    try:
        with _fetch_lock:
            logger.info("Starting EPG fetch/update")
            new_data = _build_xmltv()
            old = _read_cache()
            if old != new_data:
                _write_cache(new_data)
                logger.info("EPG cache updated")
            else:
                logger.info("EPG unchanged; no update written")
    except Exception as e:
        logger.error("Error fetching/updating EPG cache: %s", e)


def _get_xmltv():
    # Return cached copy immediately if present; otherwise None
    return _read_cache()


def schedule_daily_epg_update(time_str):
    """
    Schedule a daily update at HH:MM (24-hour) specified by time_str.
    Runs in a background daemon thread and calls _fetch_and_update_cache().
    """
    if not time_str:
        logger.info("No EPG update time configured; skipping scheduler")
        return

    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        logger.error("Invalid TVH_EPG_UPDATE_TIME format; expected HH:MM")
        return

    def run_loop():
        logger.info("EPG scheduler started; daily update at %02d:%02d", hour, minute)
        while True:
            now = datetime.now()
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                next_run = next_run + timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()
            logger.debug("EPG scheduler sleeping for %s seconds", wait_seconds)
            time_mod.sleep(wait_seconds)
            try:
                _fetch_and_update_cache()
            except Exception as e:
                logger.error("Scheduled EPG update failed: %s", e)
            # loop will compute next_run again (24h cycle)

    t = threading.Thread(target=run_loop, args=())
    t.daemon = True
    t.start()


def _ensure_initial_background_fetch():
    """If there is no cache, start a background fetch but do not block."""
    if _read_cache() is None:
        logger.info("No EPG cache found; starting initial background fetch")
        t = threading.Thread(target=_fetch_and_update_cache, args=())
        t.daemon = True
        t.start()

def _start_ssdp():
    ssdp = SSDPServer()
    thread_ssdp = threading.Thread(target=ssdp.run, args=())
    thread_ssdp.daemon = True  # Daemonize thread
    thread_ssdp.start()
    ssdp.register(
        "local",
        "uuid:{}::upnp:rootdevice".format(discoverData["DeviceID"]),
        "upnp:rootdevice",
        "http://{}:{}/device.xml".format(
            config["tvhProxyHost"], config["tvhProxyPort"]
        ),
        "SSDP Server for tvhProxy",
    )


# --- HTTP endpoints -------------------------------------------------
@app.route("/discover.json")
def discover():
    return jsonify(discoverData)


@app.route("/lineup.json")
def lineup():
    # Try to fetch channel grid from TVHeadend API
    try:
        url = "%s/api/channel/grid" % config["tvhURL"]
        params = {"start": 0, "limit": 999999}
        r = requests.get(url, params=params, auth=HTTPDigestAuth(config["tvhUser"], config["tvhPassword"]))
        entries = r.json(strict=False).get("entries", [])
        lineup_list = []
        for entry in entries:
            number = entry.get("number") or entry.get("display_number") or entry.get("uuid")
            name = entry.get("name") or entry.get("display_name") or ""
            uuid = entry.get("uuid")
            stream_url = None
            if uuid:
                stream_url = "%s/stream/channel/%s?profile=%s&weight=%s" % (
                    config["tvhURL"], uuid, config["streamProfile"], config["tvhWeight"]
                )
            lineup_list.append({"GuideNumber": str(number), "GuideName": name, "URL": stream_url})
        return jsonify(lineup_list)
    except Exception:
        # Fallback: parse cached XML
        try:
            xml_bytes = _get_xmltv()
            root = ElementTree.fromstring(xml_bytes)
            lineup_list = []
            for child in root:
                if child.tag == "channel":
                    chan_id = child.attrib.get("id")
                    display = child.find("display-name")
                    name = display.text if display is not None else ""
                    lineup_list.append({"GuideNumber": str(chan_id), "GuideName": name, "URL": None})
            return jsonify(lineup_list)
        except Exception as e:
            logger.error("Failed to generate lineup: %s", e)
            return jsonify([])


@app.route("/lineup_status.json")
def lineup_status():
    return jsonify({"ScanInProgress": False, "ScanPossible": True, "Source": "TVHeadend"})


@app.route("/epg.xml")
def epg_xml():
    data = _read_cache()
    if data:
        return Response(data, mimetype="application/xml")
    # No cached data available; start a background fetch and return 503 immediately
    _ensure_initial_background_fetch()
    return Response("", status=503)


@app.route("/device.xml")
def device_xml():
    return render_template("device.xml", device=discoverData)

def main():
    http = WSGIServer(
        (config["bindAddr"], int(config["tvhProxyPort"])),
        app.wsgi_app,
        log=logger,
        error_log=logger,
    )
    _start_ssdp()
    # Ensure we have a cache being built in the background (non-blocking)
    _ensure_initial_background_fetch()
    # Also trigger an immediate non-blocking fetch at startup
    t_init = threading.Thread(target=_fetch_and_update_cache, args=())
    t_init.daemon = True
    t_init.start()
    # Start the daily scheduler if configured
    schedule_daily_epg_update(EPG_UPDATE_TIME)
    http.serve_forever()

if __name__ == "__main__":
    main()