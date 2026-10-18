# Traccar bridge

Replays Apple FindMy locations from this repository's macless-haystack endpoint into a
[Traccar](https://www.traccar.org/) server.

## What it does

Apple's FindMy network reports are encrypted and, by design, are only available in batches
of "the last N days". Traccar, on the other hand, is a live tracker that expects devices
to push their current position. This bridge closes the gap:

```
CronJob (every 6h)
  └─ read {name: privateKey} map          (Kubernetes Secret)
  └─ derive the Apple search id per key   (sha256 of the advertisement public key)
  └─ POST /find {"ids":[...], "days":1}   (macless-haystack endpoint)
       ← encrypted reports
  └─ decrypt each report locally           (ECDH P-224 + AES-128-GCM)
  └─ drop reports already posted           (state file on a PVC)
  └─ POST each to Traccar :5055            (OsmAnd protocol, original timestamp)
```

Because the bridge sends each report's real timestamp, Traccar stores the positions at
their actual fix time and rebuilds trips/history exactly as if the tag had been streaming.
Traccar accepts historical timestamps on the OsmAnd endpoint and does not filter out past
positions by default (`filter.past` is unset), so backfilling the last 24 hours works.

## Crypto

Only the private key is needed. From it the bridge derives the Apple search id and
decrypts the reports:

- `adv_x = P-224 public key x` of the private key → `id = base64(sha256(adv_x))`.
- Each payload is `[timestamp(4)][confidence(1)][ephemeral pubkey(57)][encrypted+tag]`
  (a leading version byte is stripped for payloads longer than 88 bytes).
- `shared = ECDH(private, ephemeral)`, `key = sha256(shared || 0x00000001 || ephemeral)`,
  then AES-128-GCM with `key[:16]` and IV `key[16:]`.
- The plaintext is `lat(int32)/1e7`, `lon(int32)/1e7`, `accuracy(1)`, `status(1)`.

`bridge/test_bridge.py` covers id derivation and a full encrypt/decrypt round trip
(`python3 -m unittest -v` in `bridge/`), including negative coordinates and both payload
lengths.

## Configuration

All configuration is via environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEVICES_FILE` | – | JSON file `{name: privateKey}` (mounted Secret). |
| `DEVICES_DIR` | – | Alternative: a directory of files, filename = device name, content = private key. |
| `MH_ENDPOINT_URL` | `http://macless-haystack:6176/find` | Endpoint `/find` URL. |
| `MH_ENDPOINT_USER` / `MH_ENDPOINT_PASS` | – | Optional HTTP basic auth for the endpoint. |
| `MH_VERIFY_TLS` | `true` | Set to `false` for a self-signed endpoint certificate. |
| `TRACCAR_URL` | `http://traccar:5055` | Traccar OsmAnd endpoint. |
| `STATE_FILE` | `/state/seen.json` | Dedup state (must survive across runs). |
| `QUERY_DAYS` | `1` | How far back to query (the endpoint's `days`). |
| `STATE_MAX_AGE` | `172800` | Prune seen reports older than this many seconds. |
| `MH_PER_DEVICE` | `true` | Query each device with its own `/find` request (one log line per tag). Set to `false` to batch. |
| `MH_MAX_IDS` | `100` | Max ids per fetch request when batching (`MH_PER_DEVICE=false`). |
| `HTTP_TIMEOUT` | `60` | Request timeout in seconds. |
| `DRY_RUN` | `false` | Fetch and decrypt, log what would be posted, but do not post or touch state. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

## Build and run

```sh
docker build -t macless-haystack-bridge bridge/

docker run --rm \
  -e DEVICES_FILE=/etc/macless-bridge/devices.json \
  -e MH_ENDPOINT_URL=http://macless-haystack:6176/find \
  -e TRACCAR_URL=http://traccar:5055 \
  -e STATE_FILE=/state/seen.json \
  -v "$PWD/devices.json:/etc/macless-bridge/devices.json:ro" \
  -v "$PWD/state:/state" \
  --network mh-network \
  macless-haystack-bridge
```

The image is published by `.github/workflows/build-bridge-image.yaml` as
`ghcr.io/al26p/macless-haystack-bridge:latest` on pushes to `main`, on `v*` tags and via
*Run workflow*.

## Kubernetes

`k8s/traccar-bridge.yaml` contains everything needed and is designed to sit next to the
endpoint deployment (`k8s/macless-haystack.yaml`):

1. Put your devices into the `macless-bridge-devices` Secret. The key is the Traccar
   device name; the value is the base64 `Private key` from `generate_keys.py` (or the
   `privateKey` field of the exported `_devices.json`):

   ```sh
   kubectl create secret generic macless-bridge-devices \
     --from-literal=devices.json='{"airtag1":"<privateKey-b64>","airtag2":"<privateKey-b64>"}' \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

2. Apply the manifest and trigger a first run:

   ```sh
   kubectl apply -f k8s/traccar-bridge.yaml
   kubectl create job --from=cronjob/macless-bridge macless-bridge-manual
   kubectl logs -f job/macless-bridge-manual
   ```

The CronJob runs `0 */6 * * *` with `concurrencyPolicy: Forbid`. The small
`macless-bridge-state` PVC holds `seen.json` so the overlapping 24-hour windows never
insert duplicate positions.

## Traccar requirements and caveats

- **Devices must exist** in Traccar with a `uniqueId` equal to the Secret's key, because
  `database.registerUnknown` is off by default. If you enable it, note the default regex
  is `\w{3,15}`, which rejects hyphens and names longer than 15 characters.
- Device names become the OsmAnd `id`; avoid `/` and `\` (Traccar rejects those) and keep
  the casing consistent.
- The bridge sends `conf`, `status` and `accuracy` as position attributes and a coarse
  `batt` level derived from the status byte.
- **Timestamps are preserved.** Each position is posted with the report's original
  timestamp (in milliseconds), never the current time, so Traccar stores it at the fix
  time and rebuilds history correctly. Make sure the target devices do **not** have the
  `time.override` attribute set to `serverTime` (that would replace the fix time with the
  server time); leave it unset or use `deviceTime`.
- If the endpoint's Apple session has lapsed it returns `503 {"error":"relogin_required"}`.
  The bridge logs this and exits `0`; run `POST /login` and `POST /2fa` as described in the
  top-level README, and the next run resumes.
