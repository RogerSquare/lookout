"""Hub core: a fresh install with no config, cameras added and removed through the admin API with
the built-in go2rtc, credential masking, and importing an old hand-written config.

Run in the hub image (it has go2rtc, ffmpeg and the model):
  docker run --rm --network host -v $PWD:/app -v $PWD/testmedia:/media -w /app lookout-hub \
      python tests/test_core.py
"""

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
MEDIA = Path(os.environ.get("LOOKOUT_TEST_MEDIA", "/media"))
PORT, RTSP, API = 18766, 18554, 18984     # clear of a live hub on the same host
BASE = f"http://127.0.0.1:{PORT}"
results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}", flush=True)
    results.append(bool(ok))


def start_hub(data, legacy=None):
    env = {**os.environ, "LOOKOUT_HTTPS_PORT": "18866", "LOOKOUT_BONJOUR": "0", "LOOKOUT_DATA": str(data), "LOOKOUT_CONFIG": str(legacy or data / "none.json"),
           "LOOKOUT_PORT": str(PORT), "LOOKOUT_RTSP_PORT": str(RTSP), "LOOKOUT_GO2RTC_API_PORT": str(API),
           "LOOKOUT_VERSION": "test"}
    return subprocess.Popen([sys.executable, str(HERE.parent / "hub.py")], env=env)


async def wait_up(s):
    for _ in range(150):
        try:
            async with s.get(f"{BASE}/healthz") as r:
                if r.status == 200:
                    return await r.json()
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.2)
    return None


async def next_event(resp, pred, timeout):
    """Read SSE events off an open /events response until one matches."""
    end = time.monotonic() + timeout
    buf = ""
    while time.monotonic() < end:
        try:
            chunk = await asyncio.wait_for(resp.content.readany(), max(0.1, end - time.monotonic()))
        except asyncio.TimeoutError:
            break
        buf += chunk.decode()
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            data = [l[6:] for l in block.splitlines() if l.startswith("data: ")]
            if data:
                ev = json.loads(data[0])
                if pred(ev):
                    return ev
    return None


async def fresh_install():
    data = Path(tempfile.mkdtemp())
    proc = start_hub(data)
    try:
        async with aiohttp.ClientSession() as s:
            h = await wait_up(s)
            check("fresh hub with no config comes up healthy", h is not None and h.get("version") == "test", str(h))
            async with s.get(f"{BASE}/api/state") as r:
                st = await r.json()
                check("and has no cameras yet", st["cameras"] == [])
            settings_file = data / "settings.json"
            mode = stat.S_IMODE(settings_file.stat().st_mode) if settings_file.exists() else None
            check("settings.json created, readable only by the hub", mode == 0o600, oct(mode or 0))
            admin = (data / "admin-token").read_text().strip()
            app_token = json.loads(settings_file.read_text())["app_token"]
            A = {"Authorization": f"Bearer {admin}"}
            # new hubs only take paired apps; these tests use the old shared token
            async with s.put(f"{BASE}/api/admin/settings", json={"allow_legacy_token": True}, headers=A) as r:
                assert r.status == 200

            async with s.get(f"{BASE}/api/admin/config") as r:
                check("admin API needs the admin token", r.status == 401)
            async with s.get(f"{BASE}/api/admin/config", headers={"Authorization": f"Bearer {app_token}"}) as r:
                check("the app token is not an admin token", r.status == 401)
            async with s.get(f"{BASE}/api/admin/config", headers=A) as r:
                cfg = await r.json()
                check("admin config reports the built-in go2rtc", r.status == 200 and cfg["hub"]["go2rtc"] == "embedded",
                      json.dumps(cfg.get("hub")))

            async with s.post(f"{BASE}/api/admin/cameras", json={"id": "Bad Id", "url": "rtsp://x"}, headers=A) as r:
                check("invalid camera id rejected (400)", r.status == 400)
            async with s.post(f"{BASE}/api/admin/cameras", json={"id": "porch"}, headers=A) as r:
                check("camera without a url rejected (400)", r.status == 400)
            async with s.post(f"{BASE}/api/admin/cameras/probe", json={"url": str(MEDIA / "people.jpg")}, headers=A) as r:
                p = await r.json()
                check("probe opens a source and reports its size", p.get("ok") and p.get("width") == 810, json.dumps(p))

            # A camera fed by a looping picture of people, through the hub's own go2rtc
            events = await s.get(f"{BASE}/events", timeout=aiohttp.ClientTimeout(total=None))
            await next_event(events, lambda e: e["type"] == "state", 5)
            url = (f"exec:ffmpeg -hide_banner -loglevel error -re -loop 1 -i {MEDIA / 'people.jpg'} -r 10 "
                   "-vf scale=810:1080 -c:v libx264 -pix_fmt yuv420p -g 10 -f rtsp {output}")
            t0 = time.monotonic()
            async with s.post(f"{BASE}/api/admin/cameras", json={"id": "porch", "name": "Porch", "role": "door", "url": url},
                              headers=A) as r:
                check("camera added through the admin API", r.status == 200, str(r.status))
            st = await next_event(events, lambda e: e["type"] == "state" and any(c["id"] == "porch" for c in e["cameras"]), 5)
            check("clients get the new camera list without a restart", st is not None)
            det = await next_event(events, lambda e: e["type"] == "person_detected" and e["camera"] == "porch", 40)
            check("new camera detects people via the built-in go2rtc", det is not None,
                  f"{time.monotonic() - t0:.1f}s after adding" if det else "no detection in 40s")
            check("door role carried on the alert", det is not None and det.get("role") == "door")
            yaml = (data / "go2rtc.yaml").read_text()
            check("go2rtc config generated with the camera's restreams",
                  all(f"  porch{sfx}:" in yaml for sfx in ("", "_watch", "_phone")) and "porch_sd" not in yaml)
            sys.path.insert(0, str(HERE.parent))
            from streams import resolve
            hd = resolve({"id": "porch", "url": "rtsp://x/y"}, 8554, "hub")["hls_variants"]
            check("full-screen variant 'hd' is the camera's own restream", hd == {"phone": "porch_phone", "hd": "porch"}, str(hd))
            old = resolve({"id": "porch", "url": "rtsp://x/y", "hls_variants": {"phone": "custom"}}, 8554, "hub")
            check("a stored variant list still gets 'hd'", old["hls_variants"] == {"phone": "custom", "hd": "porch"})

            # Passwords are masked everywhere they're shown, and survive a save from the setup page
            async with s.put(f"{BASE}/api/admin/cameras/yard", json={"url": "rtsp://admin:hunter2@192.0.2.10/live"},
                             headers=A) as r:
                shown = (await r.json()).get("url")
            async with s.get(f"{BASE}/api/admin/config", headers=A) as r:
                listed = next(c["url"] for c in (await r.json())["cameras"] if c["id"] == "yard")
            check("stream password masked in API replies", shown == listed == "rtsp://admin:***@192.0.2.10/live", listed)
            async with s.put(f"{BASE}/api/admin/cameras/yard", json={"name": "Yard", "url": listed}, headers=A) as r:
                pass
            stored = next(c for c in json.loads((data / "settings.json").read_text())["cameras"] if c["id"] == "yard")
            check("saving the masked url keeps the real password", stored["url"].endswith("hunter2@192.0.2.10/live")
                  and stored["name"] == "Yard")

            async with s.get(f"{BASE}/api/watch/cameras", headers={"Authorization": f"Bearer {app_token}"}) as r:
                wc = await r.json()
                check("watch API serves the new cameras with the store's app token",
                      r.status == 200 and {c["id"] for c in wc["cameras"]} == {"porch", "yard"})
                porch = next(c for c in wc["cameras"] if c["id"] == "porch")
                check("watch API uses the camera's built-in HLS stream", porch["hls"] == "/api/watch/cameras/porch/stream.m3u8")

            async with s.delete(f"{BASE}/api/admin/cameras/yard", headers=A) as r:
                check("camera deleted", r.status == 200)
            async with s.get(f"{BASE}/api/state") as r:
                ids = {c["id"] for c in (await r.json())["cameras"]}
                check("deleted camera gone from state and go2rtc", ids == {"porch"} and "yard" not in (data / "go2rtc.yaml").read_text())
            async with s.put(f"{BASE}/api/admin/settings", json={"detect_fps": 4}, headers=A) as r:
                check("settings update accepted", r.status == 200)
            async with s.put(f"{BASE}/api/admin/settings", json={"admin_token": "x"}, headers=A) as r:
                check("tokens can't be changed through settings (400)", r.status == 400)
            events.close()
    finally:
        proc.terminate()
        try:
            proc.wait(8)
        except subprocess.TimeoutExpired:
            proc.kill()
    time.sleep(1)
    needle = str(data / "go2rtc.yaml").encode()
    leftover = [p for p in Path("/proc").iterdir() if p.name.isdigit()
                and needle in (p / "cmdline").read_bytes()] if Path("/proc").exists() else []
    check("go2rtc stops with the hub", not leftover)


async def legacy_import():
    data = Path(tempfile.mkdtemp())
    legacy = data / "config.json"
    legacy.write_text(json.dumps({"cameras": [{"id": "kitchen", "name": "Kitchen Cam", "detect_url": str(MEDIA / "empty.jpg"),
                                                "loop": True, "min_person_height": 0.25}],
                                  "watch_token": "keep-me-123", "detect_fps": 5}))
    proc = start_hub(data, legacy)
    try:
        async with aiohttp.ClientSession() as s:
            await wait_up(s)
            st = json.loads((data / "settings.json").read_text())
            check("old config.json imported on first start",
                  [c["id"] for c in st["cameras"]] == ["kitchen"] and st["cameras"][0]["role"] == "door")
            check("old app token kept, so paired apps keep working", st["app_token"] == "keep-me-123")
            check("imported hub stays on its external go2rtc", st["settings"].get("go2rtc") == "off")
            async with s.get(f"{BASE}/api/watch/cameras?token=keep-me-123") as r:
                check("watch API works with the old token", r.status == 200)
    finally:
        proc.terminate()
        proc.wait(8)


async def main():
    await fresh_install()
    await legacy_import()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
