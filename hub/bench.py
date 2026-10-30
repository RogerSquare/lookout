"""Time each model on each available ONNX Runtime provider (milliseconds per frame, median of N).
Run in the hub image: python bench.py [/models] [runs]"""

import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

models = Path(sys.argv[1] if len(sys.argv) > 1 else "/models")
runs = int(sys.argv[2]) if len(sys.argv) > 2 else 15
cands = [("yolo11s", models / "yolo11s.onnx", 640)]
meta = json.loads((models / "rfdetr.json").read_text()) if (models / "rfdetr.json").exists() else {}
cands += [(f"rfdetr-{k}", models / v["file"], v["resolution"]) for k, v in meta.items()]

providers = ort.get_available_providers()
print("providers:", providers)
plan = [("CPU", ["CPUExecutionProvider"], {})]
if "OpenVINOExecutionProvider" in providers:
    plan.append(("OpenVINO-GPU", [("OpenVINOExecutionProvider", {"device_type": "GPU"})], {}))
    plan.append(("OpenVINO-CPU", [("OpenVINOExecutionProvider", {"device_type": "CPU"})], {}))
for label, prov, _ in plan:
    for name, path, size in cands:
        if not path.exists():
            continue
        so = ort.SessionOptions()
        so.intra_op_num_threads = 4
        try:
            t = time.perf_counter()
            sess = ort.InferenceSession(str(path), so, providers=prov)
            load = time.perf_counter() - t
            x = np.random.rand(1, 3, size, size).astype(np.float32)
            feed = {sess.get_inputs()[0].name: x}
            for _ in range(3):
                sess.run(None, feed)
            times = []
            for _ in range(runs):
                t = time.perf_counter()
                sess.run(None, feed)
                times.append((time.perf_counter() - t) * 1000)
            print(f"{label:13} {name:14} {statistics.median(times):7.1f} ms  (load {load:.1f}s)", flush=True)
        except Exception as e:
            print(f"{label:13} {name:14} failed: {str(e)[:120]}", flush=True)
