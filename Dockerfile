FROM christld/macless-haystack

# os-findmy-e (MIT): emulated macOS CoreADI used for Apple anisette + login.
# Pinned so the build is reproducible; see NOTICE for attribution.
ARG OSFINDMYE_REPO=https://github.com/sacca97/os-findmy-e.git
ARG OSFINDMYE_COMMIT=2096d875dac6b5ed2347a9ae8c39c7251c0d6ed6

RUN apt-get update && apt-get install -y --no-install-recommends \
      p7zip-full ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone "$OSFINDMYE_REPO" /opt/osfindmye \
 && git -C /opt/osfindmye checkout "$OSFINDMYE_COMMIT" \
 && rm -rf /opt/osfindmye/.git

# FindMy.py @ parawanderer fork + Anisette (see os-findmy-e/requirements.txt).
# unicorn runs the genuine x86_64 CoreADI.framework.
RUN pip install --no-cache-dir -r /opt/osfindmye/requirements.txt \
 && pip install --no-cache-dir unicorn

# Downloads iTunes 12.8.3 and unpacks CoreADI.framework into /opt/osfindmye/ext
# (Apple's binary is fetched at build time and is not redistributed here).
RUN cd /opt/osfindmye && python tools/fetch_mac_coreadi.py

COPY mac_auth.py /app/endpoint/mac_auth.py
COPY mh_endpoint.py /app/endpoint/mh_endpoint.py
RUN printf '#!/bin/bash\nexit 0\n' > /app/endpoint/updateRepo && chmod +x /app/endpoint/updateRepo
