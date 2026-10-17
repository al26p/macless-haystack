# macless-haystack + os-findmy-e

A drop-in replacement for the `macless-haystack` endpoint that authenticates to Apple
using an emulated Mac (`os-findmy-e`'s CoreADI-under-Unicorn anisette) instead of an
external Anisette server, and that supports HTTP-driven Apple login (including 2FA) so it
can run unattended in Kubernetes.

It was built to work around Apple's late-2026 changes that stopped issuing a
`searchPartyToken` to non-native (Android/Windows) Anisette identities and started
enforcing platform provenance on `X-MMe-Client-Info`.

## What it does

- **Auth via emulated macOS identity.** `mac_auth.py` provisions a Mac identity through
  `os-findmy-e` (genuine `CoreADI.framework` from iTunes 12.8.3, run under Unicorn) and
  builds the anisette headers locally. No Anisette server is required.
- **HTTP login with 2FA.** No interactive terminal:
  - `POST /login` starts the Apple login (credentials from `APPLE_ID` / `APPLE_PASSWORD`).
  - `POST /2fa` with `{"code":"123456"}` submits the SMS code.
  - `GET /status` reports the state.
- **No auth loop.** When no session exists the endpoint serves but returns `503` on
  `/find`, and it never auto-logs-in. A login only happens when `/login` is called.
- **Self-healing fetch.** On an Apple `401/403`, `mac_auth.py` runs `os-findmy-e`'s
  credential-free anisette resync once and retries. If that cannot revive the machine, the
  session is dropped and `/find` returns `503 {"error":"relogin_required"}` until an
  operator calls `POST /login`.

## Files

| Path | Purpose |
| --- | --- |
| `Dockerfile` | Builds the endpoint image on top of `christld/macless-haystack`, adding `os-findmy-e`. |
| `mac_auth.py` | Anisette headers, login state machine, resync, fetch wrapper. |
| `mh_endpoint.py` | Patched macless-haystack endpoint with `/login`, `/2fa`, `/status` routes. |
| `k8s/macless-haystack.yaml` | Secret, PVC, Deployment and Service. |

## Build

```sh
docker build -t macless-haystack-osfindmye .
```

## Run (Docker)

```sh
docker run -d --name macless-haystack --restart on-failure \
  --network mh-network -p 6176:6176 \
  -v mh_data:/app/endpoint/data \
  -e APPLE_ID=you@example.com -e APPLE_PASSWORD=secret \
  macless-haystack-osfindmye
```

Then, when a login is needed (the logs say so, or `/status` shows `waiting_manual_login`):

```sh
curl -X POST localhost:6176/login          # sends the SMS
curl -X POST localhost:6176/2fa \
  -H 'Content-Type: application/json' -d '{"code":"123456"}'
curl localhost:6176/status                 # -> {"state": "authenticated", ...}
```

## Kubernetes

1. Create the credentials Secret and apply the manifest:

```sh
kubectl create secret generic apple-credentials \
  --from-literal=APPLE_ID=you@example.com \
  --from-literal=APPLE_PASSWORD=secret
kubectl apply -f k8s/macless-haystack.yaml
```

2. When a login is required, run it from inside the pod so the `Service` does not need to
   route while the endpoint is unauthenticated:

```sh
kubectl exec deploy/macless-haystack -- curl -s -X POST localhost:6176/login
kubectl exec deploy/macless-haystack -- curl -s localhost:6176/status
kubectl exec deploy/macless-haystack -- curl -s -X POST localhost:6176/2fa \
  -H 'Content-Type: application/json' -d '{"code":"123456"}'
```

The PVC holds `auth.json` and the `macadi` machine identity, so a re-login survives pod
restarts. Health probes point at `/` (always `200` while the process is alive), so a lapsed
login does not trigger a restart / crash loop; watch `/status` and `503` on `/find` instead.

> Note: the image is built from a locally-tagged `macless-haystack-osfindmye:latest`. Push it
> to your registry and set `image:` accordingly before applying.

## Credits and sources

This project builds on and patches other work. See [NOTICE](NOTICE) for the pinned
commits and licenses. In short:

- [`dchristl/macless-haystack`](https://github.com/dchristl/macless-haystack) — AGPL-3.0 (base image and the endpoint we patch).
- [`sacca97/os-findmy-e`](https://github.com/sacca97/os-findmy-e) — MIT (emulated macOS CoreADI anisette and login tooling).
- [`parawanderer/FindMy.py`](https://github.com/parawanderer/FindMy.py) — MIT (Apple ID / Find My library).
- `Anisette` 1.2.4 (Dadoum) — dependency of the FindMy.py fork.
- Apple's `CoreADI.framework` (from iTunes 12.8.3) is downloaded at build time and is not
  redistributed by this repository.

## License

AGPL-3.0. Because `mh_endpoint.py` is a modified copy of AGPL-licensed
`macless-haystack`, this repository and the image it builds are distributed under the GNU
Affero General Public License v3.0. See [LICENSE](LICENSE).
