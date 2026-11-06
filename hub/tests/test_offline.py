"""Camera offline notices: a camera with no picture for offline_alert_after seconds is reported
once (rate-limited), and "back" follows only a reported outage. Run: python tests/test_offline.py"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hub  # noqa: E402

results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}", flush=True)
    results.append(bool(ok))


T0 = 1_000_000.0


def cam(cid, loop=False):
    return SimpleNamespace(id=cid, name=cid.title(), loop=loop, latest_time=T0, started_at=T0 - 600,
                           offline=False, offline_notified=0.0, offline_reported=False)


published = []
stub = SimpleNamespace(settings={"offline_alert_after": 120, "offline_renotify": 1800},
                       cameras={"kitchen": cam("kitchen"), "bedroom": cam("bedroom"), "test": cam("test", loop=True)},
                       publish=published.append)
check_offline = lambda now: hub.Hub.check_offline(stub, now)  # noqa: E731
k, b = stub.cameras["kitchen"], stub.cameras["bedroom"]

check("fresh pictures: nothing reported", check_offline(T0 + 5) == [])
b.latest_time = T0 + 100
check("under the limit: nothing reported", check_offline(T0 + 110) == [])
ev = check_offline(T0 + 125)
check("kitchen silent 2 min: reported offline once",
      [e["camera"] for e in ev] == ["kitchen"] and ev[0]["offline"] and ev[0]["minutes"] == 2 and k.offline, str(ev))
b.latest_time = T0 + 299
check("no repeat while still offline", check_offline(T0 + 300) == [])
check("test (looping file) cameras are never reported", not stub.cameras["test"].offline)
k.latest_time = T0 + 400
ev = check_offline(T0 + 401)
check("picture again: 'back' sent", [(e["camera"], e["offline"]) for e in ev] == [("kitchen", False)] and not k.offline)
ev = check_offline(T0 + 401 + 125)
check("drops again within 30 min: marked offline, not re-notified", k.offline and not [e for e in ev if e["camera"] == "kitchen"])
k.latest_time = T0 + 700
check("its return is silent too (no 'offline' was sent)",
      not [e for e in check_offline(T0 + 701) if e["camera"] == "kitchen"] and not k.offline)
ev = check_offline(T0 + 700 + 1900)
check("after the renotify window: reported again", any(e["camera"] == "kitchen" and e["offline"] for e in ev))
stub.settings["offline_alert_after"] = 0
check("offline_alert_after 0 turns it off", check_offline(T0 + 99999) == [])
check("events were published", len(published) >= 3)
newcam = cam("garage")
newcam.latest_time, newcam.started_at = 0.0, T0
stub.cameras = {"garage": newcam}
stub.settings["offline_alert_after"] = 120
check("a camera that never connected counts from hub start", check_offline(T0 + 60) == [] and check_offline(T0 + 121) != [])


# Notice payload: phone gets it; no person category; collapses per camera
class Client:
    def __init__(self):
        self.sent = []

    async def post(self, url, json, headers):
        self.sent.append((url, json, headers))
        return SimpleNamespace(status_code=200, content=b"")


p = hub.Push.__new__(hub.Push)
p.cfg, p._client = {}, Client()
p.devices = {"tokW": {"topic": "dev.x.lookout.watchkitapp", "env": "production"},
             "tokP": {"topic": "dev.x.lookout", "env": "production"}}
p._token = lambda: "jwt"
asyncio.run(p.send_notice("kitchen", "Kitchen is offline", "No picture", "offline"))
sent = p._client.sent
check("notice goes to the phone only", len(sent) == 1 and sent[0][0].endswith("/tokP"), str([s[0] for s in sent]))
check("notice is not a person alert", "category" not in sent[0][1]["aps"] and sent[0][1]["notice"] == "offline")
check("notice collapses per camera", sent[0][2]["apns-collapse-id"] == "status-kitchen")
p.devices = {"tokW": {"topic": "dev.x.lookout.watchkitapp", "env": "production"}}
p._client = Client()
asyncio.run(p.send_notice("kitchen", "Kitchen is back", "ok", "back"))
check("watch-only household still gets it", len(p._client.sent) == 1)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
