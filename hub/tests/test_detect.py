"""Hardware profile: backends found, models timed, accuracy check rejects a backend that
miscomputes, and the choice fits the budget. Run in the hub image: python tests/test_detect.py"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import detect  # noqa: E402

results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}", flush=True)
    results.append(bool(ok))


models = os.environ.get("LOOKOUT_MODELS", "/models")
cat = detect.catalog(models)
check("catalog lists the RF-DETR models", {"rfdetr-nano", "rfdetr-small", "rfdetr-medium"} <= set(cat), str(sorted(cat)))
hw = detect.hardware()
check("hardware reported", hw["cpus"] and "cpu" in hw["backends"], str(hw))

prof = detect.profile(models, ("rfdetr",), threads=4)
by = {r["model"]: r for r in prof["results"]}
check("every RF-DETR size timed", set(by) == {"rfdetr-nano", "rfdetr-small", "rfdetr-medium"},
      ", ".join(f"{k}@{v['backend']} {v['ms']}ms" for k, v in by.items()))
check("bigger models cost more", by["rfdetr-nano"]["ms"] < by["rfdetr-medium"]["ms"])
if "openvino-cpu" in hw["backends"]:
    check("OpenVINO rejected for RF-DETR nano (miscomputes it)", by["rfdetr-nano"]["backend"] != "openvino-cpu",
          by["rfdetr-nano"]["backend"])

fake = {"results": [{"model": "a", "size": "nano", "backend": "cpu", "ms": 50},
                    {"model": "b", "size": "small", "backend": "cpu", "ms": 100},
                    {"model": "c", "size": "medium", "backend": "cpu", "ms": 200}]}
check("roomy hardware gets the biggest model that fits", detect.choose(fake, 1, 2, "balanced")["model"] == "c")
check("more cameras push it to a smaller model", detect.choose(fake, 2, 5, "balanced")["model"] == "a")
c = detect.choose(fake, 4, 5, "balanced")
check("when nothing fits, smallest model at fewer checks/s", c["model"] == "a" and c["fps"] == 2, str(c))
check("accuracy setting allows a bigger model than power",
      detect.SIZES.index(detect.choose(fake, 1, 5, "accuracy")["size"]) > detect.SIZES.index(detect.choose(fake, 1, 5, "power")["size"]))
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
