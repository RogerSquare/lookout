"""Pan/tilt: camera setting, the app and setup page endpoints, and the wyze-bridge backend
against a fake bridge. Run in the hub image: python tests/test_ptz.py"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

HERE = Path(__file__).resolve().parent
PORT, HTTPS, RTSP, API, BRIDGE = 18772, 18773, 18559, 18989, 18890
BASE = f"http://127.0.0.1:{PORT}"
results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}", flush=True)
    results.append(bool(ok))


async def fake_bridge(seen):
    """Answers like docker-wyze-bridge 3.x: GET /api/<cam>/<cmd>/<payload>"""
    async def cmd(request):
        seen.append((request.match_info["cam"], request.match_info["cmd"], request.match_info["payload"],
                     request.query.get("api")))
        if request.match_info["cam"] == "broken":
            return web.json_response({"status": "error", "response": "camera offline"})
        await asyncio.sleep(0.05)
        return web.json_response({"status": "success", "command": request.match_info["cmd"]})
    app = web.Application()
    app.router.add_get("/api/{cam}/{cmd}/{payload}", cmd)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", BRIDGE).start()
    return runner


async def main():
    seen = []
    bridge = await fake_bridge(seen)
    data = Path(tempfile.mkdtemp())
    env = {**os.environ, "LOOKOUT_DATA": str(data), "LOOKOUT_CONFIG": str(data / "none.json"), "LOOKOUT_PORT": str(PORT),
           "LOOKOUT_HTTPS_PORT": str(HTTPS), "LOOKOUT_RTSP_PORT": str(RTSP), "LOOKOUT_GO2RTC_API_PORT": str(API),
           "LOOKOUT_VERSION": "test", "LOOKOUT_BONJOUR": "0"}
    proc = subprocess.Popen([sys.executable, str(HERE.parent / "hub.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(150):
                try:
                    async with s.get(f"{BASE}/healthz") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.2)
            A = {"Authorization": "Bearer " + (data / "admin-token").read_text().strip()}
            async with s.put(f"{BASE}/api/admin/settings", json={"allow_legacy_token": True}, headers=A) as r:
                assert r.status == 200
            app = {"Authorization": "Bearer " + json.loads((data / "settings.json").read_text())["app_token"]}

            bad = {"id": "bedroom", "url": "rtsp://127.0.0.1:1/x", "ptz": {"type": "onvif", "url": "http://x"}}
            async with s.post(f"{BASE}/api/admin/cameras", json=bad, headers=A) as r:
                check("unknown ptz type refused", r.status == 400)
            cam = {"id": "bedroom", "name": "Bedroom", "url": "rtsp://127.0.0.1:1/x",
                   "ptz": {"type": "wyze-bridge", "url": f"http://127.0.0.1:{BRIDGE}/", "camera": "bedroom-cam",
                           "api_key": "bridge-secret"}}
            async with s.post(f"{BASE}/api/admin/cameras", json=cam, headers=A) as r:
                saved = await r.json()
                check("camera with pan/tilt saved, key masked", r.status == 200 and saved["ptz"]["api_key"] == "***")
            async with s.post(f"{BASE}/api/admin/cameras", json={"id": "kitchen", "url": "rtsp://127.0.0.1:1/y"},
                              headers=A) as r:
                pass
            async with s.get(f"{BASE}/api/watch/cameras", headers=app) as r:
                cams = {c["id"]: c for c in (await r.json())["cameras"]}
                check("apps see which cameras can move", cams["bedroom"]["ptz"] is True and cams["kitchen"]["ptz"] is False)

            async with s.post(f"{BASE}/api/watch/cameras/bedroom/ptz", json={"pan": -1, "tilt": 0.5}, headers=app) as r:
                check("move", r.status == 200, await r.text())
            check("bridge got a turn in degrees, with its key",
                  seen and seen[-1] == ("bedroom-cam", "rotary_degree", "-30,15", "bridge-secret"), str(seen[-1:]))
            async with s.post(f"{BASE}/api/watch/cameras/bedroom/ptz", json={"preset": 2}, headers=app) as r:
                check("saved position", r.status == 200 and seen[-1][1:3] == ("cruise_point", "2"))
            for body, why in (({"pan": 3}, "out of range"), ({"pan": 0, "tilt": 0}, "no move"),
                              ({"preset": 0}, "bad preset"), ({"pan": True}, "not a number")):
                async with s.post(f"{BASE}/api/watch/cameras/bedroom/ptz", json=body, headers=app) as r:
                    check(f"refused: {why}", r.status == 400)
            async with s.post(f"{BASE}/api/watch/cameras/kitchen/ptz", json={"pan": 1}, headers=app) as r:
                check("a fixed camera says it can't move", r.status == 404)
            async with s.post(f"{BASE}/api/watch/cameras/bedroom/ptz", json={"pan": 1}) as r:
                check("unpaired caller refused", r.status == 401)
            n = len(seen)
            rs = await asyncio.gather(*[s.post(f"{BASE}/api/watch/cameras/bedroom/ptz", json={"pan": 0.5}, headers=app)
                                        for _ in range(3)])
            codes = sorted(r.status for r in rs)
            check("one move at a time; extra taps while moving are dropped",
                  codes.count(200) >= 1 and set(codes) <= {200, 409} and len(seen) - n == codes.count(200), str(codes))
            async with s.post(f"{BASE}/api/admin/cameras/bedroom/ptz", json={"tilt": -1}, headers=A) as r:
                check("setup page test button moves it", r.status == 200 and seen[-1][2] == "0,-30")

            # saving the camera again with the masked key keeps the real one
            async with s.put(f"{BASE}/api/admin/cameras/bedroom", json={**cam, "ptz": {**cam["ptz"], "api_key": "***"}},
                             headers=A) as r:
                pass
            stored = json.loads((data / "settings.json").read_text())
            key = next(c for c in stored["cameras"] if c["id"] == "bedroom")["ptz"]["api_key"]
            check("masked key on save keeps the stored key", key == "bridge-secret")

            cam2 = {"id": "broken", "url": "rtsp://127.0.0.1:1/z",
                    "ptz": {"type": "wyze-bridge", "url": f"http://127.0.0.1:{BRIDGE}", "camera": "broken"}}
            async with s.post(f"{BASE}/api/admin/cameras", json=cam2, headers=A) as r:
                pass
            async with s.post(f"{BASE}/api/watch/cameras/broken/ptz", json={"pan": 1}, headers=app) as r:
                check("bridge errors come back as 502 with its reason", r.status == 502 and "offline" in await r.text())
            cam3 = {"id": "gone", "url": "rtsp://127.0.0.1:1/w",
                    "ptz": {"type": "wyze-bridge", "url": "http://127.0.0.1:1", "camera": "gone"}}
            async with s.post(f"{BASE}/api/admin/cameras", json=cam3, headers=A) as r:
                pass
            async with s.post(f"{BASE}/api/watch/cameras/gone/ptz", json={"pan": 1}, headers=app) as r:
                check("bridge down: 502", r.status == 502)
    finally:
        proc.terminate()
        try:
            proc.wait(8)
        except subprocess.TimeoutExpired:
            proc.kill()
        await bridge.cleanup()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
