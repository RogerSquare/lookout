"""End-to-end test: runs the real hub on file sources and checks the SSE stream and HTTP API.

Sources (from ../../tests): arrive_leave.mp4 = people for 4 s then an empty street (loops at 34 s),
people.jpg = people in every frame. Needs the model at $LOOKOUT_MODEL (default /models/yolo11s.onnx).

Run (in the hub image):
  docker run --rm -v $PWD:/app -v $PWD/../tests:/media -v ./models:/models:ro lookout-hub \
      python tests/test_hub.py
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
MEDIA = Path(os.environ.get("LOOKOUT_TEST_MEDIA", "/media"))
PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"
HIDE_AFTER = 3

results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}", flush=True)
    results.append(ok)


class Events:
    """Collects SSE events from /events in the background."""

    def __init__(self, session):
        self.session = session
        self.events = []
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        async with self.session.get(f"{BASE}/events") as resp:
            buf = ""
            async for chunk in resp.content.iter_any():
                buf += chunk.decode()
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    data = [l[6:] for l in block.splitlines() if l.startswith("data: ")]
                    if data:
                        ev = json.loads(data[0])
                        ev["_at"] = time.monotonic()
                        self.events.append(ev)

    async def wait_for(self, pred, timeout):
        end = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < end:
            for ev in self.events[seen:]:
                if pred(ev):
                    return ev
            seen = len(self.events)
            await asyncio.sleep(0.05)
        return None

    def count(self, pred, since=0.0):
        return sum(1 for ev in self.events if ev["_at"] >= since and pred(ev))


def is_(type_, cam=None):
    return lambda ev: ev["type"] == type_ and (cam is None or ev.get("camera") == cam)


def filter_checks():
    """Size and zone filters, so distant passers-by (e.g. on the street below) don't alert."""
    import av
    import numpy as np
    from PIL import Image

    sys.path.insert(0, str(HERE.parent))
    import detect
    import hub

    # The model under test: LOOKOUT_MODEL names one in the catalog (default: RF-DETR nano)
    name = os.environ.get("LOOKOUT_MODEL", "rfdetr-nano")
    spec = dict(detect.catalog(os.environ.get("LOOKOUT_MODELS", "/models"))[name], name=name)
    det = detect.Detector(spec, ["CPUExecutionProvider"], 2)
    print(f"model under test: {name}", flush=True)
    photo = Image.open(MEDIA / "people.jpg").convert("RGB")  # 810x1080, people fill the height

    def frame_of(img):
        return av.VideoFrame.from_ndarray(np.asarray(img), format="rgb24")

    near = frame_of(photo.resize((480, 640)))
    small = Image.new("RGB", (1920, 1080), (90, 90, 90))  # same people at ~15% of frame height
    small.paste(photo.resize((120, 160)), (100, 100))
    far = frame_of(small)

    near_boxes, far_boxes = det.boxes(near), det.boxes(far)
    s_near = hub.person_score(near_boxes, min_height=0.25)
    s_far_any = hub.person_score(far_boxes)
    s_far = hub.person_score(far_boxes, min_height=0.25)
    check("min_person_height keeps people who fill the frame", s_near >= 0.5, f"score {s_near:.2f}")
    check("distant people are detectable without the filter", s_far_any >= 0.3, f"score {s_far_any:.2f}")
    check("min_person_height=0.25 ignores distant people", s_far < 0.5, f"score {s_far:.2f}")
    right_half = [[0.5, 0], [1, 0], [1, 1], [0.5, 1]]
    s_zone_out = hub.person_score(far_boxes, zone=right_half)        # people are top-left
    s_zone_in = hub.person_score(far_boxes, zone=[[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5]])
    check("zone ignores people standing outside it", s_zone_out < 0.5, f"score {s_zone_out:.2f}")
    check("zone keeps people standing inside it", s_zone_in >= 0.3, f"score {s_zone_in:.2f}")
    return hub, det, near, far


def gpu_helper_checks(hub, det, near):
    """The PC's GPU helper is used when it answers and the local CPU covers it when it doesn't."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    changes = []

    # Nothing listening: fall back to the CPU for this frame, then leave the helper alone
    down = hub.GpuHelper("http://127.0.0.1:9", 0.4, det, changes.append)
    t = time.perf_counter()
    s = hub.person_score(down.boxes(near))
    first_ms = (time.perf_counter() - t) * 1000
    check("helper unreachable: CPU result used for the same frame", s >= 0.5 and down.name == det.name,
          f"score {s:.2f}, {first_ms:.0f} ms")
    t = time.perf_counter()
    down.boxes(near)
    again_ms = (time.perf_counter() - t) * 1000
    check("helper unreachable: backs off instead of retrying every frame",
          down.retry_at > time.time() + 10, f"next frame {again_ms:.0f} ms (CPU only)")

    # A fake helper that always sees one tall person (0.91) and one tiny one far away (0.95)
    class Fake(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"boxes": [[0.5, 0.5, 0.3, 0.8, 0.91], [0.1, 0.1, 0.02, 0.08, 0.95]],
                               "model": "fake", "ms": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    up = hub.GpuHelper(f"http://127.0.0.1:{srv.server_address[1]}", 0.4, det, changes.append)
    s_any = hub.person_score(up.boxes(near))
    s_door = hub.person_score(up.boxes(near), min_height=0.25)
    check("helper answering: its boxes are used and the detector reports pc-gpu",
          abs(s_any - 0.95) < 1e-3 and up.name == "pc-gpu" and "pc-gpu" in changes, f"score {s_any:.2f}")
    check("min_person_height still filters the helper's boxes (tiny far person ignored)",
          abs(s_door - 0.91) < 1e-3, f"score {s_door:.2f}")
    srv.shutdown()


TOKEN = "s3cret"


async def fake_go2rtc():
    """Answers like go2rtc's HLS API: master playlist -> media playlist -> init + segments."""
    from aiohttp import web

    requests = []
    mpegurl = "application/vnd.apple.mpegurl"

    async def master(request):
        requests.append(dict(request.query))
        if request.query.get("src") not in ("street", "street_phone"):
            return web.Response(status=404, text="stream not found")
        return web.Response(text='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=192000,CODECS="avc1.4D0029"\n'
                                 "hls/playlist.m3u8?id=AbC123\n", content_type=mpegurl)

    async def hls(request):
        requests.append({"file": request.match_info["file"], **request.query})
        name = request.match_info["file"]
        if name == "playlist.m3u8":
            return web.Response(text='#EXTM3U\n#EXT-X-MAP:URI="init.mp4?id=AbC123"\n#EXTINF:0.500,\n'
                                     "segment.m4s?id=AbC123&n=0\n", content_type=mpegurl)
        return web.Response(body=b"\x00\x00\x00\x18ftypmp42" if name == "init.mp4" else b"moof-data",
                            content_type="video/mp4" if name == "init.mp4" else "video/iso.segment")

    app = web.Application()
    app.router.add_get("/api/stream.m3u8", master)
    app.router.add_get("/api/hls/{file}", hls)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}", requests


async def fake_apns():
    """Answers like APNs: 200 for live tokens, 410 Unregistered for tokens starting 'dead'."""
    from aiohttp import web

    pushes = []

    async def device(request):
        token = request.match_info["token"]
        pushes.append({"token": token, "headers": dict(request.headers), "body": await request.json()})
        if token.startswith("dead"):
            return web.json_response({"reason": "Unregistered"}, status=410)
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/3/device/{token}", device)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", pushes


async def push_checks(s, pushes, data_dir):
    """Apps register push tokens; a person alert is pushed to each, gone tokens are dropped."""
    auth = {"Authorization": f"Bearer {TOKEN}"}
    live, dead = "ab" * 32, "dead" + "00" * 30
    async with s.post(f"{BASE}/api/watch/devices", json={"token": live, "topic": "dev.rogersquare.lookout.watchkitapp"}) as r:
        check("push: device registration needs the token", r.status == 401)
    async with s.post(f"{BASE}/api/watch/devices", json={"token": "nothex!", "topic": "dev.rogersquare.lookout"},
                      headers=auth) as r:
        check("push: bad token rejected (400)", r.status == 400)
    async with s.post(f"{BASE}/api/watch/devices", json={"token": "ab" * 32, "topic": "com.evil.app"},
                      headers=auth) as r:
        check("push: foreign topic rejected (400)", r.status == 400)
    for t in (live, dead):
        async with s.post(f"{BASE}/api/watch/devices", headers=auth,
                          json={"token": t, "topic": "dev.rogersquare.lookout.watchkitapp", "env": "production"}) as r:
            assert r.status == 200, await r.text()
    pushes.clear()
    async with s.post(f"{BASE}/api/test", json={"camera": "street", "seconds": 1}) as r:
        pass
    for _ in range(50):
        if len(pushes) >= 2:
            break
        await asyncio.sleep(0.1)
    sent = {p["token"]: p for p in pushes}
    p = sent.get(live, {})
    h = {k.lower(): v for k, v in p.get("headers", {}).items()}
    check("push: person alert pushed to each registered device", set(sent) == {live, dead}, str(list(sent)))
    check("push: APNs headers (bearer JWT, topic, alert type, priority 10)",
          h.get("authorization", "").startswith("bearer ey") and h.get("apns-topic") == "dev.rogersquare.lookout.watchkitapp"
          and h.get("apns-push-type") == "alert" and h.get("apns-priority") == "10", str({k: h.get(k) for k in ("apns-topic", "apns-push-type")}))
    body = p.get("body", {})
    check("push: payload has title, camera name, sound and camera id",
          body.get("camera") == "street" and body["aps"]["alert"]["subtitle"] == "Street"
          and body["aps"]["sound"] == "default", json.dumps(body)[:160])
    check("push: alerts are time-sensitive", body.get("aps", {}).get("interruption-level") == "time-sensitive")
    snap = body.get("snapshot", "")
    check("push: payload points at the detection snapshot", snap.startswith("/api/watch/cameras/street/snapshot.jpg"), snap)
    async with s.get(f"{BASE}{snap}&w=300&token={TOKEN}") as r:
        data = await r.read()
        from PIL import Image
        import io
        size = Image.open(io.BytesIO(data)).size if r.status == 200 else None
        check("snapshot.jpg serves the detection frame at the watch width", size is not None and size[0] == 300, str(size))
    async with s.get(f"{BASE}{snap}") as r:
        check("snapshot.jpg needs the token", r.status == 401)
    phone = "cd" * 32
    async with s.post(f"{BASE}/api/watch/devices", headers=auth,
                      json={"token": phone, "topic": "dev.rogersquare.lookout", "env": "production"}) as r:
        pass
    pushes.clear()
    async with s.post(f"{BASE}/api/test", json={"camera": "street", "seconds": 1}) as r:
        pass
    await asyncio.sleep(1)
    check("push: by default only the iPhone gets alerts (it mirrors to the watch; no doubles)",
          {p["token"] for p in pushes} == {phone}, str([p["token"][:4] for p in pushes]))
    pushes.clear()
    async with s.post(f"{BASE}/api/test", json={"camera": "street", "seconds": 1, "push": "phone"}) as r:
        pass
    await asyncio.sleep(1)
    check("push: a test can target the iPhone app", {p["token"] for p in pushes} == {phone},
          str([p["token"][:4] for p in pushes]))
    await asyncio.sleep(0.3)
    saved = json.loads((data_dir / "devices.json").read_text())
    check("push: token Apple reports gone (410) is dropped", live in saved and dead not in saved, str(list(saved)))

    # Live Activity: someone at a door camera
    start_tok, act_tok = "e1" * 40, "f2" * 40
    async with s.post(f"{BASE}/api/watch/activity-token", headers=auth,
                      json={"kind": "start", "token": start_tok, "topic": "com.evil", "env": "production"}) as r:
        check("live activity: foreign topic refused", r.status == 400)
    async with s.post(f"{BASE}/api/watch/activity-token", headers=auth,
                      json={"kind": "start", "token": start_tok, "topic": "dev.rogersquare.lookout", "env": "production"}) as r:
        check("live activity: push-to-start token registered", r.status == 200)
    pushes.clear()
    async with s.post(f"{BASE}/api/test", json={"camera": "street", "seconds": 1}) as r:
        pass
    await asyncio.sleep(1.5)
    check("live activity: not for a camera that isn't a door", not any(p["token"] == start_tok for p in pushes))
    pushes.clear()
    async with s.post(f"{BASE}/api/test", json={"camera": "photo", "seconds": 2}) as r:
        pass
    for _ in range(30):
        if any(p["token"] == start_tok for p in pushes):
            break
        await asyncio.sleep(0.1)
    la = next((p for p in pushes if p["token"] == start_tok), {})
    h = {k.lower(): v for k, v in la.get("headers", {}).items()}
    aps = la.get("body", {}).get("aps", {})
    check("live activity: started at a door camera (push-to-start, liveactivity topic)",
          h.get("apns-push-type") == "liveactivity" and h.get("apns-topic") == "dev.rogersquare.lookout.push-type.liveactivity"
          and aps.get("event") == "start" and aps.get("attributes-type") == "PersonActivityAttributes"
          and aps.get("attributes") == {"cameraID": "photo", "name": "Photo"} and aps["content-state"]["person"] is True,
          json.dumps(la.get("body"))[:200])
    async with s.post(f"{BASE}/api/watch/activity-token", headers=auth,
                      json={"kind": "update", "camera": "photo", "token": act_tok, "topic": "dev.rogersquare.lookout"}) as r:
        check("live activity: the running activity's token registered", r.status == 200)
    for _ in range(60):
        if any(p["token"] == act_tok for p in pushes):
            break
        await asyncio.sleep(0.1)
    end = next((p for p in pushes if p["token"] == act_tok), {}).get("body", {}).get("aps", {})
    check("live activity: ended when they leave", end.get("event") == "end" and end["content-state"]["person"] is False
          and end.get("dismissal-date"), json.dumps(end)[:200])


async def watch_checks(s, go2rtc_requests):
    """The watch/iPhone routes: token, camera list, scaled frames, and the HLS proxy."""
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with s.get(f"{BASE}/api/watch/cameras") as r:
        check("watch: no token -> 401", r.status == 401)
    async with s.get(f"{BASE}/api/watch/cameras", headers={"Authorization": "Bearer wrong"}) as r:
        check("watch: wrong token -> 401", r.status == 401)
    async with s.get(f"{BASE}/api/state") as r:
        check("watch token doesn't affect the existing client routes", r.status == 200)
    tunnel = {"Cf-Connecting-Ip": "203.0.113.9"}
    async with s.get(f"{BASE}/api/state", headers=tunnel) as r:
        check("via the tunnel: LAN-only routes don't exist (404)", r.status == 404)
    async with s.post(f"{BASE}/api/test", json={"camera": "street"}, headers=tunnel) as r:
        check("via the tunnel: can't fire test alerts (404)", r.status == 404)
    async with s.get(f"{BASE}/api/watch/cameras", headers=tunnel) as r:
        check("via the tunnel: app routes still need the token (401)", r.status == 401)
    async with s.get(f"{BASE}/api/watch/cameras", headers={**tunnel, "Authorization": f"Bearer {TOKEN}"}) as r:
        check("via the tunnel: app routes work with the token", r.status == 200)

    async with s.get(f"{BASE}/api/watch/cameras", headers=auth) as r:
        body = await r.json()
        cams = {c["id"]: c for c in body["cameras"]}
        st = cams.get("street", {})
        check("watch: camera list with frame + hls urls",
              r.status == 200 and set(cams) == {"street", "photo"}
              and st.get("frame") == "/api/watch/cameras/street/frame.jpg"
              and st.get("hls") == "/api/watch/cameras/street/stream.m3u8", json.dumps(st))
        check("watch: last_seen recorded", bool(st.get("last_seen")), f"last_seen={st.get('last_seen')}")
        check("watch: camera without a restream has no hls url", cams.get("photo", {}).get("hls") is None)

    async with s.get(f"{BASE}/api/watch/cameras/street/frame.jpg?w=200&token={TOKEN}") as r:
        data = await r.read()
        from PIL import Image
        import io
        size = Image.open(io.BytesIO(data)).size if r.status == 200 else None
        check("watch: frame.jpg?w=200 is a 200 px wide JPEG (token in query works)",
              size is not None and size[0] == 200, str(size))
    async with s.get(f"{BASE}/api/watch/cameras/street/frame.jpg?w=abc", headers=auth) as r:
        check("watch: bad width -> 400", r.status == 400)

    async with s.get(f"{BASE}/api/watch/cameras/street/stream.m3u8?token={TOKEN}") as r:
        master = await r.text()
        check("watch: stream.m3u8 proxies go2rtc's master playlist for the camera's stream",
              r.status == 200 and r.content_type == "application/vnd.apple.mpegurl"
              and {"src": "street", "mp4": ""} in go2rtc_requests, master.strip().splitlines()[-1] if master else "")
        check("watch: playlist URIs carry the token", f"hls/playlist.m3u8?id=AbC123&token={TOKEN}" in master)
    async with s.get(f"{BASE}/api/watch/cameras/street/stream.m3u8?v=phone&token={TOKEN}") as r:
        check("watch: ?v=phone serves the camera's phone variant",
              r.status == 200 and {"src": "street_phone", "mp4": ""} in go2rtc_requests)
    async with s.get(f"{BASE}/api/watch/cameras", headers=auth) as r:
        street = next(c for c in (await r.json())["cameras"] if c["id"] == "street")
        check("watch: cameras list the HLS variants they offer", street.get("hls_variants") == ["phone"],
              str(street.get("hls_variants")))
    async with s.get(f"{BASE}/api/watch/cameras/street/stream.m3u8?v=nope&token={TOKEN}") as r:
        check("watch: unknown variant falls back to the default stream", r.status == 200)
    # Follow the playlist the way AVPlayer would: relative URIs, token only in the query
    async with s.get(f"{BASE}/api/watch/cameras/street/hls/playlist.m3u8?id=AbC123&token={TOKEN}") as r:
        media = await r.text()
        check("watch: media playlist proxied, init + segment URIs carry the token",
              r.status == 200 and f'URI="init.mp4?id=AbC123&token={TOKEN}"' in media
              and f"segment.m4s?id=AbC123&n=0&token={TOKEN}" in media)
    async with s.get(f"{BASE}/api/watch/cameras/street/hls/segment.m4s?id=AbC123&n=0&token={TOKEN}") as r:
        seg = await r.read()
        check("watch: segments proxied byte for byte, token not forwarded to go2rtc",
              r.status == 200 and seg == b"moof-data"
              and {"file": "segment.m4s", "id": "AbC123", "n": "0"} in go2rtc_requests)
    # MJPEG: read a few parts off the open stream the way the watch does (Content-Length framing)
    async with s.get(f"{BASE}/api/watch/cameras/street/live.mjpeg?w=320&fps=10&token={TOKEN}") as r:
        buf, frames, t0 = b"", [], time.monotonic()
        while len(frames) < 5 and time.monotonic() - t0 < 5:
            buf += await r.content.read(65536)
            while b"\r\n\r\n" in buf:
                head, rest = buf.split(b"\r\n\r\n", 1)
                length = int(next(l.split(b":")[1] for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")))
                if len(rest) < length + 2:
                    break
                frames.append(rest[:length])
                buf = rest[length + 2:]
        elapsed = time.monotonic() - t0
        from PIL import Image
        import io
        size = Image.open(io.BytesIO(frames[0])).size if frames else None
        check("watch: live.mjpeg streams JPEG parts at the requested width",
              r.content_type == "multipart/x-mixed-replace" and len(frames) == 5 and size and size[0] == 320,
              f"{len(frames)} frames in {elapsed:.2f}s, {size}")
        check("watch: live.mjpeg respects the fps cap", elapsed >= 0.35, f"{elapsed:.2f}s for 5 frames at 10 fps")
    async with s.post(f"{BASE}/api/watch/alerts", json={"camera": "photo", "enabled": False}) as r:
        check("watch: POST /api/watch/alerts needs the token", r.status == 401)
    async with s.post(f"{BASE}/api/watch/alerts", json={"camera": "photo", "enabled": False}, headers=auth) as r:
        body = await r.json()
        check("watch: POST /api/watch/alerts switches a camera's alerts", r.status == 200 and body["cameras"]["photo"] is False)
    async with s.post(f"{BASE}/api/watch/alerts", json={"camera": "photo", "enabled": True}, headers=auth) as r:
        pass
    async with s.post(f"{BASE}/api/test", json={"camera": "photo", "seconds": 2}) as r:
        pass
    async with s.get(f"{BASE}/api/watch/cameras", headers=auth) as r:
        during = {c["id"]: c["person"] for c in (await r.json())["cameras"]}
    await asyncio.sleep(2.5)
    async with s.post(f"{BASE}/api/alerts", json={"camera": "photo", "enabled": False}) as r:
        pass  # stop real detection on the photo so only the test state is measured
    await asyncio.sleep(0.7)
    async with s.get(f"{BASE}/api/watch/cameras", headers=auth) as r:
        after = {c["id"]: c["person"] for c in (await r.json())["cameras"]}
    async with s.post(f"{BASE}/api/alerts", json={"camera": "photo", "enabled": True}) as r:
        pass
    check("watch: a test alert shows as person for its duration, then clears",
          during.get("photo") is True and after.get("photo") is False, f"during={during} after={after}")
    async with s.get(f"{BASE}/api/watch/cameras/street/live.mjpeg") as r:
        check("watch: live.mjpeg needs the token", r.status == 401)
    async with s.get(f"{BASE}/api/watch/cameras/street/hls/..%2Fsecret?token={TOKEN}") as r:
        check("watch: only HLS file names are proxied", r.status == 404)


async def main():
    hub_mod, det, near, _ = filter_checks()
    gpu_helper_checks(hub_mod, det, near)
    go2rtc_runner, go2rtc_url, go2rtc_requests = await fake_go2rtc()
    apns_runner, apns_url, pushes = await fake_apns()
    tmp = Path(tempfile.mkdtemp())
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    (tmp / "apns.p8").write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    config = {
        "cameras": [
            {"id": "street", "name": "Street", "detect_url": str(MEDIA / "arrive_leave.mp4"), "loop": True,
             "streams": {"sd": "rtsp://example:8555/street"}, "hls_variants": {"phone": "street_phone"}},
            {"id": "photo", "name": "Photo", "detect_url": str(MEDIA / "people.jpg"), "loop": True, "role": "door"},
        ],
        "model": os.environ.get("LOOKOUT_MODEL", "rfdetr-nano"),
        "hide_after_seconds": HIDE_AFTER,
        "host": "127.0.0.1",
        "port": PORT,
        "go2rtc_api": go2rtc_url,
        "watch_token": TOKEN,
        "apns": {"key_file": str(tmp / "apns.p8"), "key_id": "TESTKEY01", "team_id": "TEAM000001", "url": apns_url},
    }
    (tmp / "config.json").write_text(json.dumps(config))
    # Photo alerts start OFF (persisted state), so the street camera is tested on its own first
    (tmp / "state.json").write_text(json.dumps({"enabled": True, "cameras": {"photo": False}}))
    env = {**os.environ, "LOOKOUT_HTTPS_PORT": "18868", "LOOKOUT_BONJOUR": "0", "LOOKOUT_CONFIG": str(tmp / "config.json"), "LOOKOUT_DATA": str(tmp)}
    proc = subprocess.Popen([sys.executable, str(HERE.parent / "hub.py")], env=env)

    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(100):
                try:
                    async with s.get(f"{BASE}/healthz") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)
            t0 = time.monotonic()
            ev = Events(s)
            await ev.start()

            first = await ev.wait_for(lambda e: True, 5)
            check("first SSE event is the full state", first is not None and first["type"] == "state"
                  and {c["id"] for c in first["cameras"]} == {"street", "photo"})
            check("persisted per-camera alerts restored (photo off)",
                  first is not None and first["alerts"]["cameras"] == {"street": True, "photo": False})

            det = await ev.wait_for(is_("person_detected", "street"), 8)
            check("street: person_detected while people are in frame", det is not None,
                  f"{det['_at'] - t0:.1f}s after subscribe, conf {det['confidence']}" if det else "")
            if det:
                check("event carries name, confidence, streams and snapshot url",
                      det["name"] == "Street" and det["confidence"] >= 0.5 and det["streams"]
                      and det["snapshot"].startswith("/api/cameras/street/snapshot.jpg") and "time" in det)
                async with s.get(BASE + det["snapshot"]) as r:
                    body = await r.read()
                    check("snapshot url serves the detection frame as JPEG",
                          r.status == 200 and r.content_type == "image/jpeg" and body[:2] == b"\xff\xd8",
                          f"{len(body)} bytes")

            gone = await ev.wait_for(is_("person_gone", "street"), 4 + HIDE_AFTER + 6)
            check(f"street: person_gone ~{HIDE_AFTER}s after the people leave", gone is not None
                  and gone["reason"] == "left", f"{gone['_at'] - t0:.1f}s, duration {gone['duration']}s" if gone else "")

            check("photo: no alert while its alerts are off",
                  ev.count(is_("person_detected", "photo")) == 0)

            # Turn the photo camera's alerts on through the API
            async with s.post(f"{BASE}/api/alerts", json={"camera": "photo", "enabled": True}) as r:
                check("POST /api/alerts per-camera on", r.status == 200 and (await r.json())["cameras"]["photo"])
            a = await ev.wait_for(lambda e: e["type"] == "alerts" and e["cameras"]["photo"], 3)
            check("alerts change is broadcast to subscribers", a is not None)
            pd = await ev.wait_for(is_("person_detected", "photo"), 5)
            check("photo: person_detected once its alerts are on", pd is not None)

            # Global off: the raised alert is withdrawn and inference stops
            async with s.post(f"{BASE}/api/alerts", json={"enabled": False}) as r:
                check("POST /api/alerts global off", r.status == 200 and not (await r.json())["enabled"])
            off = await ev.wait_for(lambda e: is_("person_gone", "photo")(e) and e["reason"] == "alerts_off", 3)
            check("global off withdraws the photo alert (person_gone reason=alerts_off)", off is not None)
            t_off = time.monotonic()
            await asyncio.sleep(3)
            check("no detections while alerts are globally off",
                  ev.count(is_("person_detected"), since=t_off) == 0)

            async with s.post(f"{BASE}/api/alerts", json={"enabled": True}) as r:
                pass
            back = await ev.wait_for(lambda e: is_("person_detected", "photo")(e) and e["_at"] > t_off + 3, 5)
            check("global on: detection resumes", back is not None)

            persisted = json.loads((tmp / "state.json").read_text())
            check("alerts switch persisted to state.json",
                  persisted == {"enabled": True, "cameras": {"photo": True}}, json.dumps(persisted))

            async with s.post(f"{BASE}/api/alerts", json={"enabled": "yes"}) as r:
                check("bad request body is rejected (400)", r.status == 400)
            async with s.post(f"{BASE}/api/alerts", json={"enabled": True, "camera": "nope"}) as r:
                check("unknown camera is rejected (404)", r.status == 404)
            async with s.get(f"{BASE}/api/cameras/street/live.jpg") as r:
                check("live.jpg serves the newest frame", r.status == 200 and (await r.read())[:2] == b"\xff\xd8")
            async with s.get(f"{BASE}/api/state") as r:
                st = await r.json()
                check("GET /api/state reports both cameras connected",
                      all(c["connected"] for c in st["cameras"]))
            await watch_checks(s, go2rtc_requests)
            await push_checks(s, pushes, tmp)
            # Stop the hub with the SSE stream still open, as `docker stop` would
            t_stop = time.monotonic()
            proc.terminate()
            try:
                proc.wait(8)
                check("hub exits promptly on SIGTERM with a client connected",
                      True, f"{time.monotonic() - t_stop:.1f}s")
            except subprocess.TimeoutExpired:
                check("hub exits promptly on SIGTERM with a client connected", False, "still running after 8s")
            ev.task.cancel()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        await go2rtc_runner.cleanup()
        await apns_runner.cleanup()

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
