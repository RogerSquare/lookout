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


POWER = {"pancam": "on", "fixedcam": "on"}


async def fake_bridge(seen):
    """Answers like docker-wyze-bridge 3.x: GET /api, /api/<cam>/<cmd>[/<payload>]"""
    def keyed(request):
        return request.query.get("api") == "k"

    async def listing(request):
        if not keyed(request):
            return web.Response(status=401)
        return web.json_response({"available": 2, "enabled": 2, "total": 2, "cameras": {
            "pancam": {"product_model": "HL_PAN3", "nickname": "Pan", "mac": "AA"},
            "fixedcam": {"product_model": "HL_CAM4", "nickname": "Fixed", "mac": "BB"}}})

    async def get(request):
        cam, what = request.match_info["cam"], request.match_info["cmd"]
        seen.append((cam, what, None, request.query.get("api")))
        if what == "power":
            return web.json_response({"status": "success", "value": POWER.get(cam, "on")})
        if what == "cruise_points":
            return web.json_response({"status": "success", "value": [{"horizontal": 177, "vertical": 83}]})
        return web.json_response({"status": "success", "value": 1})

    async def cmd(request):
        if request.match_info["cmd"] == "power" and request.match_info["cam"] in POWER:
            POWER[request.match_info["cam"]] = request.match_info["payload"]
        seen.append((request.match_info["cam"], request.match_info["cmd"], request.match_info["payload"],
                     request.query.get("api")))
        if request.match_info["cam"] == "broken":
            return web.json_response({"status": "error", "response": "camera offline"})
        await asyncio.sleep(0.05)
        return web.json_response({"status": "success", "command": request.match_info["cmd"]})
    app = web.Application()
    app.router.add_get("/api", listing)
    app.router.add_get("/api/{cam}/{cmd}", get)
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
           "LOOKOUT_VERSION": "test", "LOOKOUT_BONJOUR": "0", "LOOKOUT_SCHEDULE_EVERY": "1"}
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

            # ---- the hub's camera bridge: controls for every camera it streams ----
            for cid, stream in (("pan1", "pancam"), ("fixed", "fixedcam")):
                async with s.post(f"{BASE}/api/admin/cameras", json={"id": cid, "url": f"rtsp://127.0.0.1:1/{stream}"},
                                  headers=A) as r:
                    pass
            async with s.put(f"{BASE}/api/admin/bridge", json={"url": f"http://127.0.0.1:{BRIDGE}", "api_key": "nope"},
                             headers=A) as r:
                check("bridge with a wrong key refused, with the reason", r.status == 400 and "key" in await r.text())
            async with s.put(f"{BASE}/api/admin/bridge", json={"url": f"http://127.0.0.1:{BRIDGE}/", "api_key": "k"},
                             headers=A) as r:
                check("bridge saved", r.status == 200, await r.text())
            async with s.get(f"{BASE}/api/admin/bridge", headers=A) as r:
                b = await r.json()
                check("bridge settings shown, key masked", b["api_key"] == "***" and set(b["bridge_cameras"]) == {"pancam", "fixedcam"})
            async with s.get(f"{BASE}/api/watch/cameras", headers=app) as r:
                cams = {c["id"]: c for c in (await r.json())["cameras"]}
            check("pan camera: sleep, night vision, status light, pan/tilt and presets",
                  cams["pan1"]["caps"] == ["sleep", "night_vision", "status_light", "ptz", "presets"] and cams["pan1"]["ptz"] is True,
                  str(cams["pan1"]["caps"]))
            check("fixed camera: no pan/tilt", cams["fixed"]["caps"] == ["sleep", "night_vision", "status_light"]
                  and cams["fixed"]["ptz"] is False)
            check("camera the bridge doesn't have: no controls", cams["kitchen"]["caps"] == [])
            check("older pan/tilt-only setting still works", cams["bedroom"]["caps"] == ["ptz", "presets"])
            check("nobody asleep", not any(c["asleep"] for c in cams.values()))

            async with s.post(f"{BASE}/api/watch/cameras/fixed/control", json={"action": "sleep"}, headers=app) as r:
                check("sleep", r.status == 200 and (await r.json())["asleep"] is True)
            check("bridge switched it off, with the hub's key", seen[-1] == ("fixedcam", "power", "off", "k"), str(seen[-1]))
            async with s.get(f"{BASE}/api/watch/cameras", headers=app) as r:
                check("apps see it asleep", next(c for c in (await r.json())["cameras"] if c["id"] == "fixed")["asleep"] is True)
            async with s.post(f"{BASE}/api/watch/cameras/fixed/control", json={"action": "wake"}, headers=app) as r:
                check("a second command right away: busy (409) with a sentence",
                      r.status == 409 and "moment" in await r.text())
            await asyncio.sleep(2.1)
            async with s.post(f"{BASE}/api/watch/cameras/fixed/control", json={"action": "wake"}, headers=app) as r:
                check("wake", r.status == 200 and (await r.json())["asleep"] is False and seen[-1][1:3] == ("power", "on"))
            async with s.post(f"{BASE}/api/watch/cameras/pan1/control", json={"action": "night_vision", "value": "auto"},
                              headers=app) as r:
                check("night vision auto", r.status == 200 and seen[-1][:3] == ("pancam", "night_vision", "auto"))
            for body, code, why in (({"action": "explode"}, 400, "unknown action"),
                                    ({"action": "night_vision", "value": "maybe"}, 400, "bad value")):
                await asyncio.sleep(2.1)
                async with s.post(f"{BASE}/api/watch/cameras/pan1/control", json=body, headers=app) as r:
                    check(f"refused: {why}", r.status == code, str(r.status))
            async with s.post(f"{BASE}/api/watch/cameras/kitchen/control", json={"action": "sleep"}, headers=app) as r:
                check("a camera without controls says so (404)", r.status == 404)
            async with s.post(f"{BASE}/api/watch/cameras/pan1/ptz", json={"pan": 1}, headers=app) as r:
                check("pan/tilt through the hub's bridge", r.status == 200 and seen[-1][:3] == ("pancam", "rotary_degree", "30,0"))
            async with s.get(f"{BASE}/api/watch/cameras/pan1/ptz", headers=app) as r:
                check("saved positions listed", r.status == 200 and (await r.json())["presets"] == [{"pan": 177, "tilt": 83}])
            async with s.post(f"{BASE}/api/watch/cameras/pan1/ptz", json={"save_preset": 2}, headers=app) as r:
                check("saving a position: not supported yet, said plainly", r.status == 501 and "Wyze app" in await r.text())
            await asyncio.sleep(2.1)
            async with s.post(f"{BASE}/api/admin/cameras/fixed/control", json={"action": "status_light", "value": "off"},
                              headers=A) as r:
                check("setup page test button", r.status == 200 and seen[-1][:3] == ("fixedcam", "status_light", "off"))

            # ---- sleep schedules ----
            for body, why in (({"sleep": "25:00", "wake": "07:00"}, "bad time"),
                              ({"sleep": "07:00", "wake": "07:00"}, "same times"),
                              ({"sleep": "21:00", "wake": "07:00", "days": [9]}, "bad day"),
                              ({"sleep": "21:00", "wake": "07:00", "tz": "Mars/Base"}, "bad time zone")):
                async with s.put(f"{BASE}/api/watch/cameras/fixed/schedule", json=body, headers=app) as r:
                    check(f"schedule refused: {why}", r.status == 400, str(r.status))
            async with s.put(f"{BASE}/api/watch/cameras/kitchen/schedule", json={"sleep": "21:00", "wake": "07:00"},
                             headers=app) as r:
                check("a camera that can't sleep gets no schedule (404)", r.status == 404)
            import datetime as _d
            now = _d.datetime.now(_d.timezone.utc)
            at = now.strftime("%H:%M")
            later = (now + _d.timedelta(hours=1)).strftime("%H:%M")
            await asyncio.sleep(2.1)
            n = len(seen)
            async with s.put(f"{BASE}/api/watch/cameras/fixed/schedule",
                             json={"sleep": at, "wake": later, "tz": "UTC"}, headers=app) as r:
                check("schedule saved", r.status == 200 and (await r.json())["schedule"]["days"] == list(range(7)))
            async with s.get(f"{BASE}/api/watch/cameras/fixed/schedule", headers=app) as r:
                check("schedule read back", (await r.json())["schedule"]["sleep"] == at)
            for _ in range(40):
                if any(x[:3] == ("fixedcam", "power", "off") for x in seen[n:]):
                    break
                await asyncio.sleep(0.25)
            check("the hub put the camera to sleep on schedule",
                  any(x[:3] == ("fixedcam", "power", "off") for x in seen[n:]), str(seen[n:]))
            for _ in range(30):      # the hub marks it asleep once the bridge has answered
                async with s.get(f"{BASE}/api/watch/cameras", headers=app) as r:
                    fx = next(c for c in (await r.json())["cameras"] if c["id"] == "fixed")
                if fx["asleep"]:
                    break
                await asyncio.sleep(0.1)
            check("apps see it asleep, with its schedule", fx["asleep"] is True and fx["sleep_schedule"]["wake"] == later)
            await asyncio.sleep(2)
            check("sent once, not every tick", sum(1 for x in seen[n:] if x[:3] == ("fixedcam", "power", "off")) == 1)
            async with s.put(f"{BASE}/api/watch/cameras/fixed/schedule", json={}, headers=app) as r:
                check("schedule removed", r.status == 200 and (await r.json())["schedule"] is None)
            await asyncio.sleep(2.1)
            async with s.post(f"{BASE}/api/watch/cameras/fixed/control", json={"action": "wake"}, headers=app) as r:
                pass

            POWER["pancam"] = "off"                  # switched off elsewhere (the Wyze app)
            async with s.get(f"{BASE}/api/admin/bridge?refresh=1", headers=A) as r:
                check("bridge refresh sees a camera switched off elsewhere", (await r.json())["cameras"]["pan1"]["asleep"] is True)
            async with s.put(f"{BASE}/api/admin/bridge", json={}, headers=A) as r:
                check("bridge turned off", r.status == 200)
            async with s.get(f"{BASE}/api/watch/cameras", headers=app) as r:
                cams = {c["id"]: c for c in (await r.json())["cameras"]}
                check("no bridge: no controls, nobody asleep", cams["pan1"]["caps"] == [] and not cams["pan1"]["asleep"])
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
