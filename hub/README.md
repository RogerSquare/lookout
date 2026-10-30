# Lookout Hub

The always-on part of Lookout: it watches the cameras for people (or follows Frigate's alerts),
serves the setup page, and is what the iPhone, Apple Watch, PC and TV apps talk to. One Docker
container; everything it keeps lives in `/data`.

Setting one up: see the [install guide](https://rogersquare.github.io/lookout/install.html). This
file is for working on the hub itself.

## What's inside

| File | |
|---|---|
| `hub.py` | The server (aiohttp): cameras, detection loop, alerts, push, setup page API, app API |
| `store.py` | `settings.json`: settings, cameras, tokens, paired devices (file mode 0600) |
| `streams.py` | The embedded [go2rtc](https://github.com/AlexxIT/go2rtc): one connection per camera, restreamed to everything else |
| `detect.py` | Person detection on ONNX Runtime: RF-DETR nano/small/medium, a speed and accuracy test per backend at first start, and the choice of model and checks per second for this machine |
| `frigate.py` | Frigate client: cameras, review items over its websocket, snapshots, recent visitors |
| `auth.py` | Setup page sign-in: one-time setup code, scrypt password, sessions, lockout |
| `pairing.py` | App pairing: hub identity and TLS certificate, one-time codes, device keys, signed requests, media tokens |
| `web/` | The setup page |

## Ports

| Port | |
|---|---|
| 8765 | HTTP: setup page and admin API, LAN client feed (`/events`, `/api/state`), app media (with media tokens) |
| 8766 | HTTPS with the hub's self-signed certificate (pinned by the apps): the app API |
| 8555 | RTSP restreams, `rtsp://<hub>:8555/<camera id>` (`_sd` for the sub-stream) |
| 1985 | go2rtc's API, on 127.0.0.1 only |

Requests that arrive through a Cloudflare tunnel (they carry `Cf-Connecting-Ip`) only reach
`/api/watch/*`; everything else answers 404.

## App API (`/api/watch/*`)

Paired devices sign every request:

```
Authorization: Lookout <device id>:<unix time>:<base64 DER ECDSA P-256 signature>
signature over  "<METHOD>\n<path?query exactly as sent>\n<unix time>"
```

Signatures more than 120 s off, or seen before, are refused. URLs that a player opens by itself
(HLS, MJPEG, images) carry `?token=<media token>` from `GET /api/watch/media-token` instead; media
tokens last 10 minutes and stop working when the device is removed. Hubs upgraded from early
builds also accept the old shared `app_token` while `allow_legacy_token` is on.

| | |
|---|---|
| `GET /api/watch/hub` | Open. Hub id, name, version, certificate fingerprint, ports, home and away addresses |
| `POST /api/watch/pair` | Open. `{"code", "name", "platform", "public_key"}` (base64 X9.63 P-256) returns `{"device_id", "hub"}`. The 6-digit code is refused through the tunnel; 5 wrong codes a minute pause pairing |
| `POST /api/watch/pairing-code` | A one-time code for another of this person's devices (the iPhone pairs its watch with it) |
| `POST /api/watch/unpair` | The device removes itself |
| `GET /api/watch/media-token` | `{"token", "expires"}` |
| `GET /api/watch/cameras` | Cameras with `person`, `since`, `last_seen`, `alerts` and their `frame`/`hls`/`mjpeg` paths, plus the alert switches |
| `POST /api/watch/alerts` | `{"enabled": false}` for all cameras, or with `"camera": "<id>"` for one |
| `POST /api/watch/devices` | Register an APNs push token for this device |
| `GET /api/watch/cameras/{id}/frame.jpg?w=` | Newest frame, scaled |
| `GET /api/watch/cameras/{id}/snapshot.jpg?w=` | The frame that raised the latest alert |
| `GET /api/watch/cameras/{id}/stream.m3u8?v=` | HLS from go2rtc (`v=watch`/`phone` variants) |
| `GET /api/watch/cameras/{id}/live.mjpeg?w=&fps=` | MJPEG (the watch draws it itself) |
| `GET /api/watch/cameras/{id}/visitors` | Recent person alerts (Frigate source) with thumbnails |

Bonjour: `_lookout._tcp` on the HTTPS port, TXT `id`, `n` (name), `a` (address), `p` (HTTPS
port), `http` (HTTP port), `fp` (certificate fingerprint prefix), `v`.

## Setup page API (`/api/admin/*`)

Signed-in session cookie plus an `X-Lookout-CSRF: 1` header on changes, or
`Authorization: Bearer <data/admin-token>` for scripts. Cameras (`/cameras`, CRUD and `probe`),
`/settings`, `/detector`, `/benchmark`, `/source` (built-in or Frigate), `/pairing` (new code with
QR), `/devices` (list, `DELETE /devices/{id}`), `/discover`, `/restart`, `/password`.

## LAN client feed

For the PC and TV apps, on the home network only and without a login: `GET /events` (Server-Sent
Events: `state`, `person_detected`, `person_gone`, `alerts`, `camera_status`, `detector`,
`devices`), `GET /api/state`, `POST /api/alerts`, `GET /api/cameras/{id}/snapshot.jpg|live.jpg`,
`GET /healthz`.

## Settings

Everything is set on the setup page and kept in `data/settings.json`. Environment variables that
override it: `LOOKOUT_PORT`, `LOOKOUT_HTTPS_PORT`, `LOOKOUT_RTSP_PORT`, `LOOKOUT_GO2RTC_API_PORT`,
`LOOKOUT_PUBLIC_HOST` (the address apps use; set by the Docker Desktop installers),
`LOOKOUT_HUB_NAME`, `LOOKOUT_BONJOUR=0`, `LOOKOUT_DATA`.

`python hub.py --reset-password` (inside the container) forgets the setup page password; the next
start prints a new setup code.

**Push alerts** use Apple's push service with the app developer's APNs key in `/secrets` (see
`.env.example`). Hubs without it still alert the apps while they're open; a push relay for
everyone else is planned.

## Build and test

```sh
docker build -t lookout-hub:dev .
docker run --rm --network host -v $PWD:/app -w /app lookout-hub:dev python tests/test_pairing.py
```

The models are downloaded at build time from the `models-v1` release and checked against
`models.sha256` (`tools/export_rfdetr.py` makes them). `test_pairing`, `test_setup` and
`test_detect` run anywhere (CI runs them on every push). `test_core`, `test_hub` and
`test_frigate` also need short recordings with people in them, mounted at `/media`
(`-v $PWD/testmedia:/media`; `arrive_leave.mp4` and `people.jpg`), which aren't in the repository.
