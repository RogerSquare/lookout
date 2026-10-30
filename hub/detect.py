"""Person detectors and the hardware profile that picks one.

Models live in the models folder with a catalog (models.json, or the older rfdetr.json):
    rfdetr-nano / -small / -medium   RF-DETR, Apache-2.0 (the default family)
    yolo11s                          YOLO11, AGPL-3.0 (personal installs only, if present)

At setup (and on demand) the hub times each model on the best ONNX Runtime backend it has
(CUDA > OpenVINO GPU > OpenVINO CPU > plain CPU) and picks the most accurate one whose cost fits
the budget for the cameras it runs; while running it backs off if detection falls behind.
"""

import json
import logging
import os
import platform
import statistics
import threading
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

log = logging.getLogger("lookout-hub")

NO_BOXES = np.zeros((0, 5), dtype=np.float32)
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Share of one second that detection may use, per power setting
BUDGET = {"power": 0.3, "balanced": 0.55, "accuracy": 0.8}
SIZES = ["nano", "small", "medium", "large"]


def catalog(models_dir):
    """{name: {family, file, resolution, person_class, license, size}} for the models present."""
    d = Path(models_dir)
    out = {}
    for meta_name in ("models.json", "rfdetr.json"):
        f = d / meta_name
        if f.exists():
            for key, m in json.loads(f.read_text()).items():
                name = m.get("name") or f"{m.get('family', 'rfdetr')}-{key}"
                if (d / m["file"]).exists():
                    out[name] = {"family": m.get("family", "rfdetr"), "size": m.get("size", key), **m,
                                 "path": str(d / m["file"])}
            break
    if (d / "yolo11s.onnx").exists() and "yolo11s" not in out:
        out["yolo11s"] = {"family": "yolo", "size": "small", "file": "yolo11s.onnx", "resolution": 640,
                          "person_class": 0, "license": "AGPL-3.0", "path": str(d / "yolo11s.onnx")}
    return out


def backends():
    """Backends usable on this machine, best first: [(label, providers)]."""
    avail = ort.get_available_providers()
    out = []
    if "CUDAExecutionProvider" in avail:
        out.append(("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"]))
    if "OpenVINOExecutionProvider" in avail:
        devices = []
        try:
            import openvino
            devices = openvino.Core().available_devices
        except Exception:
            pass
        if any(d.startswith("GPU") for d in devices):
            out.append(("openvino-gpu", [("OpenVINOExecutionProvider", {"device_type": "GPU"}), "CPUExecutionProvider"]))
        out.append(("openvino-cpu", [("OpenVINOExecutionProvider", {"device_type": "CPU"}), "CPUExecutionProvider"]))
    if "CoreMLExecutionProvider" in avail:
        out.append(("coreml", ["CoreMLExecutionProvider", "CPUExecutionProvider"]))
    out.append(("cpu", ["CPUExecutionProvider"]))
    return out


def hardware():
    """What the setup page shows about this machine."""
    mem = None
    try:
        mem = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (ValueError, OSError, AttributeError):
        pass
    return {"arch": platform.machine(), "cpus": os.cpu_count(), "memory_gb": mem,
            "gpu_device": Path("/dev/dri/renderD128").exists() or Path("/dev/nvidia0").exists(),
            "backends": [b for b, _ in backends()]}


class Detector:
    """One ONNX person model on one backend. Thread-safe; boxes() returns (N, 5) arrays of
    cx, cy, w, h, score as fractions of the frame."""

    def __init__(self, spec, providers, threads=2, backend="cpu"):
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.spec = spec
        self.family = spec["family"]
        self.res = int(spec["resolution"])
        self.person = int(spec["person_class"])
        self.session = ort.InferenceSession(spec["path"], so, providers=providers)
        self.input = self.session.get_inputs()[0].name
        self.outputs = [o.name for o in self.session.get_outputs()]
        self.lock = threading.Lock()
        self.name = f"{backend}:{spec.get('name') or Path(spec['path']).stem}"
        self.ms = None                    # moving average inference time

    def boxes(self, frame):
        t = time.perf_counter()
        out = self._rfdetr(frame) if self.family == "rfdetr" else self._yolo(frame)
        ms = (time.perf_counter() - t) * 1000
        self.ms = ms if self.ms is None else self.ms * 0.9 + ms * 0.1
        return out

    def _rfdetr(self, frame):
        # Stretched to the square input (how RF-DETR was trained); boxes come back as fractions
        img = frame.reformat(width=self.res, height=self.res, format="rgb24").to_ndarray()
        x = ((img.astype(np.float32) / 255 - _MEAN) / _STD).transpose(2, 0, 1)[None]
        with self.lock:
            res = dict(zip(self.outputs, self.session.run(None, {self.input: x})))
        dets = res.get("dets", next(v for v in res.values() if v.shape[-1] == 4))[0]
        logits = res.get("labels", next(v for v in res.values() if v.shape[-1] > 4))[0]
        score = 1 / (1 + np.exp(-logits[:, self.person]))
        keep = score > 0.05
        if not keep.any():
            return NO_BOXES
        return np.concatenate([dets[keep], score[keep, None]], axis=1).astype(np.float32)

    def _yolo(self, frame):
        # Letterboxed to 640x640 with grey bars (as Ultralytics); output (84, 8400)
        s = self.res
        scale = s / max(frame.width, frame.height)
        w, h = max(2, round(frame.width * scale)), max(2, round(frame.height * scale))
        img = frame.reformat(width=w, height=h, format="rgb24").to_ndarray()
        canvas = np.full((s, s, 3), 114, dtype=np.uint8)
        top, left = (s - h) // 2, (s - w) // 2
        canvas[top:top + h, left:left + w] = img
        x = (canvas.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
        with self.lock:
            out = self.session.run(None, {self.input: x})[0][0]
        keep = out[4] > 0.05
        if not keep.any():
            return NO_BOXES
        cx, cy, bw, bh, sc = out[0][keep], out[1][keep], out[2][keep], out[3][keep], out[4][keep]
        return np.stack([(cx - left) / w, (cy - top) / h, bw / w, bh / h, sc], axis=1)


_reference = {}   # model path -> plain-CPU outputs for the check input


def _agrees(a, b):
    """Same answers within float noise? (mean relative difference of every output)"""
    for x, y in zip(a, b):
        x, y = np.asarray(x, np.float32), np.asarray(y, np.float32)
        if x.shape != y.shape or np.mean(np.abs(x - y)) / (np.mean(np.abs(x)) + 1e-6) > 0.02:
            return False
    return True


def benchmark(spec, backend, providers, threads=2, seconds=1.5):
    """Median ms per frame for this model on this backend - or None if it won't load, or if its
    answers differ from plain CPU on the same input (some accelerators miscompute some models:
    OpenVINO turns RF-DETR nano's 0.95 person into 0.13, for one)."""
    try:
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        sess = ort.InferenceSession(spec["path"], so, providers=providers)
        r = int(spec["resolution"])
        x = np.random.default_rng(7).random((1, 3, r, r), dtype=np.float32) * 2 - 1
        feed = {sess.get_inputs()[0].name: x}
        out = sess.run(None, feed)
        if backend != "cpu":
            if spec["path"] not in _reference:
                ref = ort.InferenceSession(spec["path"], so, providers=["CPUExecutionProvider"])
                _reference[spec["path"]] = ref.run(None, feed)
            if not _agrees(_reference[spec["path"]], out):
                log.warning("benchmark: %s on %s gives different results from plain CPU - not used",
                            spec.get("name"), backend)
                return None
        sess.run(None, feed)
        times, end = [], time.perf_counter() + seconds
        while time.perf_counter() < end or len(times) < 3:
            t = time.perf_counter()
            sess.run(None, feed)
            times.append((time.perf_counter() - t) * 1000)
        return round(statistics.median(times), 1)
    except Exception as e:
        log.info("benchmark: %s on %s unavailable: %s", spec.get("name"), backend, str(e)[:120])
        return None


def profile(models_dir, families=("rfdetr",), threads=2):
    """Time every allowed model on the best backend that runs it. Returns the profile dict
    that gets stored and shown: hardware, and [{model, backend, ms}] best-first per model."""
    cat = {k: v for k, v in catalog(models_dir).items() if v["family"] in families}
    results = []
    for name, spec in cat.items():
        spec = {**spec, "name": name}
        for backend, providers in backends():
            ms = benchmark(spec, backend, providers, threads)
            if ms is not None:
                results.append({"model": name, "family": spec["family"], "size": spec["size"],
                                "backend": backend, "ms": ms})
                break                       # backends are ordered best-first
    log.info("detector profile: %s", ", ".join(f"{r['model']}@{r['backend']} {r['ms']}ms" for r in results))
    return {"hardware": hardware(), "results": results, "time": time.time()}


def choose(prof, cameras, fps, power="balanced"):
    """Most accurate model that fits: cameras x fps x ms must stay under the power budget.
    If even the smallest doesn't fit, use it at a lower rate. Returns {model, backend, ms, fps}."""
    budget = BUDGET.get(power, BUDGET["balanced"]) * 1000
    runs = max(1, cameras) * fps
    ranked = sorted(prof["results"], key=lambda r: SIZES.index(r["size"]) if r["size"] in SIZES else 9)
    if not ranked:
        return None
    fitting = [r for r in ranked if r["ms"] * runs <= budget]
    if fitting:
        return {**fitting[-1], "fps": fps}
    smallest = ranked[0]
    slow_fps = max(1, int(budget / (smallest["ms"] * max(1, cameras))))
    return {**smallest, "fps": min(fps, slow_fps)}
