"""go2rtc restream health check: only restreams already pulling from their camera are probed,
cameras are never contacted directly, strikes, cooldown, and the restart after HEALTH_STRIKES bad
checks in a row (probe, go2rtc API and process faked). Run: python tests/test_streams_health.py"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import streams  # noqa: E402

results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}", flush=True)
    results.append(bool(ok))


class Proc:
    returncode = None

    def poll(self):
        return None


CONFIG = '''api:
  listen: "127.0.0.1:1985"
streams:
  kitchen: "rtsp://u:p@10.0.0.5/1"
  kitchen_sd: "rtsp://u:p@10.0.0.5/2"
  kitchen_watch: "exec:ffmpeg -i rtsp://127.0.0.1:8555/kitchen_sd {output}"
  bedroom: rtsp://10.0.0.6/live
log:
  level: warn
'''

check("probes camera restreams, not the exec: variants",
      streams.restreams(CONFIG) == ["kitchen", "kitchen_sd", "bedroom"], str(streams.restreams(CONFIG)))

g = streams.Go2rtc("/tmp", 1985, 8555, binary="go2rtc")
g._text, g.proc, g.started_at = CONFIG, Proc(), time.time() - 600
restarts = []
g._restart = lambda: (restarts.append(1), setattr(g, "strikes", {}))
bad = {"kitchen"}                       # restreams giving no picture
live = {"kitchen", "kitchen_sd", "bedroom"}  # restreams go2rtc is pulling right now
probed = []
g.live_producers = lambda: None if live is None else set(live)


def fake_probe(url, timeout=15):
    probed.append(url)
    return url.rsplit("/", 1)[1] not in bad


streams.has_video = fake_probe
check("first bad check only counts a strike", g.check_health() == ["kitchen"] and not restarts and g.strikes["kitchen"] == 1)
check("probes go through the local restream port", probed[0] == "rtsp://127.0.0.1:8555/kitchen")
g.check_health()
check("second bad check in a row restarts go2rtc", restarts == [1])
check("a camera is never contacted directly", all(u.startswith("rtsp://127.0.0.1:8555/") for u in probed), str(set(probed)))
bad.clear()
g.check_health()
check("healthy streams reset their strikes", restarts == [1] and all(v == 0 for v in g.strikes.values()))
bad.add("bedroom")
g.check_health()
bad.clear()
g.check_health()
bad.add("bedroom")
g.check_health()
check("a single blip between good checks never restarts", restarts == [1], str(g.strikes))
bad.clear()
g.check_health()

# Offline or unwatched camera: go2rtc isn't pulling it, so it is not probed at all
probed.clear()
bad, live = {"kitchen", "kitchen_sd"}, {"bedroom"}
for _ in range(4):
    g.check_health()
check("a camera go2rtc isn't pulling is never probed", probed and all(u.endswith("/bedroom") for u in probed), str(set(probed)))
check("an offline camera never restarts go2rtc", restarts == [1])
live = None
probed.clear()
check("go2rtc API not answering: no checks", g.check_health() == [] and not probed)
live = {"kitchen", "kitchen_sd", "bedroom"}
bad = {"bedroom"}
g.check_health(); g.check_health()
check("restarts are rate-limited (cooldown)", restarts == [1], str(restarts))
g.healed_at = time.time() - streams.HEALTH_COOLDOWN
g.check_health()
check("after the cooldown a bad restream restarts again", restarts == [1, 1])
bad = set()
g.started_at = time.time()
check("no checks in the first minute after a (re)start", g.check_health() == [])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
