FROM python:3.13-slim

WORKDIR /app

# Install build deps and cleanup

# Install system build deps needed to compile gevent/cffi/greenlet on some arches,
# upgrade packaging tools, install Python deps, then remove build deps to keep
# the final image small.
COPY requirements.txt /app/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       pkg-config \
       libffi-dev \
       libssl-dev \
       python3-dev \
    && pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --prefer-binary -r /app/requirements.txt \
    && apt-get remove -y --purge build-essential pkg-config python3-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

COPY tvhProxy.py ssdp.py requirements.txt templates/ /app/

EXPOSE 5004

ENV PYTHONUNBUFFERED=1

CMD ["python", "tvhProxy.py"]
