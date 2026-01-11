# tvhProxy

tvhProxy exposes a minimal HDHomeRun-compatible HTTP interface that proxies channels and EPG
from a running TVHeadend instance so clients (like HDHomeRun apps or Plex DVR) can discover
and stream channels via TVHeadend.

This repository contains the Python service `tvhProxy.py` which reads configuration from
environment variables (suitable for container deployment) and exposes endpoints such as
`/discover.json`, `/lineup.json`, `/lineup_status.json`, `/epg.xml`, and `device.xml`.

## Endpoints

Brief descriptions of the HTTP endpoints provided by the proxy:

- `/discover.json`: Returns device discovery metadata in JSON used by clients to find the device on the network (contains `DeviceID`, `BaseURL`, `LineupURL`, `TunerCount`, etc.). Often consumed during SSDP/UPnP discovery flows.

- `/lineup.json`: Returns the channel lineup as a JSON array. Each entry includes `GuideNumber`, `GuideName`, and a `URL` that points to the streaming endpoint for that channel (the URL references the configured `TVH_URL` and includes profile/weight parameters).
    - Note: the proxy requests the full TVHeadend channel grid (large `limit`) so the returned lineup should include all mapped channels rather than being limited to the API default page size.

- `/lineup_status.json`: Returns a small JSON object describing the lineup scan/status (e.g. `ScanInProgress`, `ScanPossible`, `Source`). This is used by clients to understand whether guide population is available.

- `/epg.xml`: Serves XMLTV EPG data (XML) built from TVHeadend's XMLTV export and EPG grid. Clients (Plex, media servers) fetch this for program guide data.
    - The service returns a cached `TVH_CACHE_FILE` immediately for `/epg.xml` requests. On startup an immediate non-blocking fetch is triggered (and a background fetch runs daily at the time configured by `TVH_EPG_UPDATE_TIME`, default `00:00`). If no cache exists yet the endpoint returns HTTP 503 while an initial background fetch runs.

- `/device.xml`: Serves the UPnP device description XML rendered from the device template. SSDP discovery responses reference this URL so clients can obtain human-readable device details.

## Key points

- Configuration is read from environment variables only.
- The web service listens on the configured port (default `5004`).
- A `Dockerfile` is provided for building a container image.
 - The service no longer uses a `.env` file or `python-dotenv`—use container environment variables.

## Required environment variables

Set these as container environment variables. Defaults shown in parentheses are used when
the variable is not provided.

- `DEVICE_ID` ("12345678"): Device identifier returned via discovery.
- `TVH_URL` ("http://localhost:9981"): URL of your TVHeadend instance.
- `TVH_PROXY_URL` (none): Full public base URL that clients should use (for example
    `https://example.com/tvhproxy` or `http://192.0.2.10:5004`). If set, this exact URL
    is used for `BaseURL` and `LineupURL` returned to clients. Use `TVH_PROXY_URL` when the
    proxy is fronted by a reverse-proxy, NAT, or a public hostname—it takes precedence
    over `TVH_PROXY_HOST`/`TVH_PROXY_PORT`.

- `TVH_PROXY_HOST` (auto-detected host IP): Hostname or IP used to construct the
    `BaseURL` when `TVH_PROXY_URL` is not provided. The service will combine this with
    `TVH_PROXY_PORT` to form `http://<TVH_PROXY_HOST>:<TVH_PROXY_PORT>` for discovery and
    device XML.

- `TVH_PROXY_PORT` (5004): Port used to build the `BaseURL` when `TVH_PROXY_URL` is
    not provided. This is also the internal port the application will listen on (default
    5004). To change the port the service binds to, set `TVH_PROXY_PORT` and start the
    container with the matching container port exposed/mapped. Example — to have the app
    listen on container port `8095` and map host port `8080` to it:

```bash
docker run -e TVH_PROXY_PORT=8095 -p 8080:8095 your-registry/tvhproxy:latest
```

Note: the `EXPOSE` line in the `Dockerfile` is informational only; the process inside
the container must actually bind to the container port you map at runtime.
- `TVH_USER` (""): TVHeadend username (if required).
- `TVH_PASSWORD` (""): TVHeadend password (if required).
- `TVH_TUNER_COUNT` (1): Number of tuners to report to clients.
- `TVH_WEIGHT` (300): Weight/priority used when generating stream URLs.
- `TVH_CHUNK_SIZE` (1048576): Chunk size used internally (bytes).
- `TVH_PROFILE` ("pass"): Stream profile for adhoc transcoding in TVHeadend.
- `TVH_CACHE_FILE` ("epg.xml"): Local path where the processed EPG XML is stored. The service will return this cached copy immediately on `/epg.xml` requests and update it in the background when fetching new data from TVHeadend. You can set an absolute path (for example `/data/epg.xml`) and bind-mount a volume for persistence.
 - `TVH_EPG_UPDATE_TIME` ("00:00"): Daily time (HH:MM, 24-hour) when the service will fetch and refresh the EPG cache from TVHeadend. Defaults to midnight. The service also triggers an immediate non-blocking fetch at startup.

## Docker Compose example

Below is a minimal `docker-compose.yml` demonstrating how to run the proxy and pass
environment variables to it:

```yaml
version: "3"
services:
    tvhproxy:
        image: prengineer/tvhproxy:latest
        container_name: tvhproxy
        restart: unless-stopped
        ports:
            - "5004:5004"
        environment:
            TZ: "America/New_York"
            # Fake HDHomeRun Device ID
            DEVICE_ID: "TVHP0001"
            # Where to serve the Proxy
            TVH_PROXY_URL: "http://10.0.0.73:5004"
            # OR the parts:
            #TVH_PROXY_HOST: "10.0.0.73"
            #TVH_PROXY_PORT: "5004"
            # Where TVHeadend is being served
            TVH_URL: "http://10.0.0.73:9981"
            # The TVHeadend user
            TVH_USER: "plex"
            # The TVHeadend password
            TVH_PASSWORD: "password"
            # How many concurrent streams you are allowed
            TVH_TUNER_COUNT: "1"
            # Pass the URLs and don't transcode
            TVH_PROFILE: "pass"
            # Local EPG cache file (mount a volume to persist between restarts)
            TVH_CACHE_FILE: "/data/epg.xml"
        volumes:
            # Mount a host directory to /data and persist the EPG file
            - /volume4/docker/Containers/TVHProxy/data:/data
            # Or mount a single file directly (host-file:container-file)
            # - /volume4/docker/Containers/TVHProxy/epg.xml:/data/epg.xml
```
 
Note: Setting the `TZ` environment variable will affect the process timezone used by most applications. Some minimal base images (including certain `-slim` images) may not include the system timezone data package (`tzdata`); for full system timezone support you can either install `tzdata` in the image or bind-mount the host's `/etc/localtime` into the container.
## Building the container (multi-arch)

You can build multi-architecture images using Docker Buildx. Example command to build
for `linux/amd64`, `linux/arm/v7`, and `linux/arm64/v8` (armv8):

```bash
# Create/ensure a buildx builder
docker buildx create --use --name multi-builder || true
docker buildx inspect --bootstrap

docker buildx build \
    --platform linux/amd64,linux/arm/v7,linux/arm64/v8 \
    -t your-registry/tvhproxy:latest \
    --push \
    .
```

## Notes:
- `--push` uploads the multi-arch image to the registry. Omit to build locally.
- Ensure your chosen base image (`python:3.13-slim`) supports the target platforms.

## Running locally without Docker
Install dependencies and run with Python (recommended virtualenv):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python tvhProxy.py
```

## ChangeLog

* [Joel Kaaberg](https://github.com/jkaberg/tvhProxy):
  * Original author and creator of tvhProxy (initial HDHomeRun-compatible proxy implementation for TVHeadend).

* [huncrys](https://hub.docker.com/r/huncrys/tvhproxy): Community Docker image and packaging updates.
  * Fixes the issue of Plex randomly dropping the device.
  * EPG export, including adding dummy programming entries for channels without EPG so you can still use these channels in Plex (see below for Plex configuration URL)
  * Configuration of variables via dotenv file

* [PREngineer](https://github.com/PREngineer/tvhProxy): Container-focused updates and reliability improvements.
    * Removed `.env` / `python-dotenv` usage — configuration now uses container environment variables only.
    * Pruned development-only dependencies from `requirements.txt` and packaging manifests.
    * Added a `Dockerfile`.
    * Reduced image surface by copying only required runtime files into the image.
    * Set the service bind address to `0.0.0.0`.
    * Changed default `TVH_TUNER_COUNT` to `1`.
    * Implemented EPG caching (`TVH_CACHE_FILE`) and immediate cached `/epg.xml` responses.
    * Added a configurable daily EPG refresh (`TVH_EPG_UPDATE_TIME`, default `00:00`) and a non-blocking startup fetch so the container updates EPG on start.
    * `/lineup.json` requests the full TVHeadend channel grid (large `limit`) so large installations return all channels.

## License and credits

See the `LICENSE` file in this repository.

## IMPORTANT

Plex is **VERY PICKY** about the channel metadata.  If you have more than 1 channel with the same ID it **WILL REFUSE** to add anything.  Make sure that you do not have duplicates in any of the channel metadata.