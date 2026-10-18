FROM python:3.14-slim

# macless-haystack endpoint (AGPL-3.0), built from the pinned upstream/ submodule.
WORKDIR /app
COPY upstream/macless-haystack/endpoint/requirements.txt /tmp/macless-requirements.txt
RUN pip install --no-cache-dir -r /tmp/macless-requirements.txt
COPY upstream/macless-haystack/endpoint /app/endpoint

# os-findmy-e (MIT) + Apple CoreADI.framework. Build-time tools and the DMG
# intermediates are removed in the same layer so they never reach the image.
ARG OSFINDMYE_REPO=https://github.com/sacca97/os-findmy-e.git
ARG OSFINDMYE_COMMIT=2096d875dac6b5ed2347a9ae8c39c7251c0d6ed6

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git p7zip-full curl ca-certificates; \
    git clone "$OSFINDMYE_REPO" /opt/osfindmye; \
    git -C /opt/osfindmye checkout "$OSFINDMYE_COMMIT"; \
    rm -rf /opt/osfindmye/.git; \
    pip install --no-cache-dir -r /opt/osfindmye/requirements.txt; \
    pip install --no-cache-dir unicorn; \
    cd /opt/osfindmye; \
    python tools/fetch_mac_coreadi.py; \
    rm -rf ext/itunes/mac/iTunes12.8.3.dmg ext/itunes/mac/ipkg; \
    find ext/itunes/mac -type f -name 'CoreADI.i386' -delete; \
    rm -f /usr/local/lib/python3.14/site-packages/unicorn/lib/libunicorn.a; \
    rm -rf /usr/local/lib/python3.14/site-packages/unicorn/include; \
    apt-get purge -y --auto-remove git p7zip-full curl; \
    rm -rf /var/lib/apt/lists/* /root/.cache /tmp/macless-requirements.txt

COPY mac_auth.py /app/endpoint/mac_auth.py
COPY mh_endpoint.py /app/endpoint/mh_endpoint.py
RUN printf '#!/bin/bash\nexit 0\n' > /app/endpoint/updateRepo && chmod +x /app/endpoint/updateRepo

CMD ["sh", "-c", "./endpoint/updateRepo && python -u endpoint/mh_endpoint.py"]
