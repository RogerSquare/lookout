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
                       publish=published.append, asleep=set())
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
sleeper = cam("porch")
stub.cameras = {"porch": sleeper}
stub.asleep = {"porch"}
check("a camera put to sleep is never reported offline", check_offline(T0 + 5000) == [] and not sleeper.offline)
solar = cam("solar")
solar.on_demand = True
stub.cameras = {"solar": solar}
stub.asleep = set()
check("an on-demand camera is never reported offline", check_offline(T0 + 5000) == [] and not solar.offline)

# ---- sleep schedules: which sleeps/wakes are due ----
from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

CHI = ZoneInfo("America/Chicago")
sched = {"bedroom": {"enabled": True, "sleep": "21:00", "wake": "07:00", "days": [0, 1, 2, 3, 4], "tz": "America/Chicago"}}
sstub = SimpleNamespace(store=SimpleNamespace(schedules=lambda: sched), cameras={"bedroom": cam("bedroom")},
                        caps=lambda c: ["sleep"], _schedule_done=set(), SCHEDULE_WINDOW=180)
due = lambda y, mo, d, h, mi: [(c.id, a) for c, a, k in hub.Hub.due_schedule_actions(  # noqa: E731
    sstub, _dt(y, mo, d, h, mi, tzinfo=CHI).timestamp())]
# 2026-11-09 is a Monday
check("sleep fires at its time on a schedule day (Monday 21:01)", due(2026, 11, 9, 21, 1) == [("bedroom", "sleep")])
check("not before it", due(2026, 11, 9, 20, 58) == [])
check("not long after it (missed by more than the window)", due(2026, 11, 9, 21, 10) == [])
check("overnight wake fires the next morning (Tuesday 07:00)", due(2026, 11, 10, 7, 0) == [("bedroom", "wake")])
check("no sleep on a day off (Saturday)", due(2026, 11, 14, 21, 1) == [])
check("Saturday-morning wake still follows Friday night's sleep", due(2026, 11, 14, 7, 1) == [("bedroom", "wake")])
check("no wake on Monday morning (Sunday had no sleep)", due(2026, 11, 9, 7, 1) == [])
ts = _dt(2026, 11, 9, 21, 1, tzinfo=CHI).timestamp()
for c, a, k in hub.Hub.due_schedule_actions(sstub, ts):
    sstub._schedule_done.add(k)
check("each one fires once", hub.Hub.due_schedule_actions(sstub, ts + 30) == [])
sched["bedroom"]["enabled"] = False
check("a schedule turned off does nothing", due(2026, 11, 10, 21, 1) == [])
sched["bedroom"] = {"enabled": True, "sleep": "09:00", "wake": "18:00", "days": [5, 6], "tz": "America/Chicago"}
check("same-day wake (weekend 9-18): wake on Saturday 18:00", due(2026, 11, 14, 18, 1) == [("bedroom", "wake")])
sstub.caps = lambda c: []
check("a camera that can't sleep is skipped", due(2026, 11, 14, 9, 1) == [])


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

# Person alerts follow alerts_to, falling back to whatever app is paired
both = {"tokW": {"topic": "dev.x.lookout.watchkitapp", "env": "production"},
        "tokP": {"topic": "dev.x.lookout", "env": "production"}}


def routed(alerts_to, devices):
    p.devices, p._client, p.alerts_to = dict(devices), Client(), alerts_to
    asyncio.run(p.send("kitchen", "Kitchen"))
    return sorted(u.rsplit("/", 1)[1] for u, _, _ in p._client.sent)


check("alerts_to phone: iPhone only", routed("phone", both) == ["tokP"])
check("alerts_to watch: watch only", routed("watch", both) == ["tokW"])
check("alerts_to all: both", routed("all", both) == ["tokP", "tokW"])
check("default (unset) is the iPhone", routed(None, both) == ["tokP"])
check("phone chosen, only a watch paired: watch still alerted",
      routed("phone", {"tokW": both["tokW"]}) == ["tokW"])

import store  # noqa: E402
st = store.Store.__new__(store.Store)
try:
    store.Store.update_settings(st, {"alerts_to": "tv"})
    check("bad alerts_to rejected", False)
except store.ConfigError:
    check("bad alerts_to rejected", True)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
