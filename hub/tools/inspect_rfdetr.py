"""Check an exported RF-DETR ONNX: I/O shapes, which class index fires on people, and scores on
the hub's test images (people.jpg should score high, empty.jpg low)."""

import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

MEDIA = Path(__file__).resolve().parents[1] / "tests"
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

for size in sys.argv[1:] or ["nano", "small", "medium"]:
    sess = ort.InferenceSession(f"rfdetr-{size}.onnx", providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    res = inp.shape[2]
    print(size, "input", inp.name, inp.shape, "outputs", [(o.name, o.shape) for o in sess.get_outputs()])
    for img_name in ("people.jpg", "empty.jpg"):
        img = Image.open(MEDIA / img_name).convert("RGB").resize((res, res))
        x = ((np.asarray(img, dtype=np.float32) / 255 - MEAN) / STD).transpose(2, 0, 1)[None]
        outs = dict(zip([o.name for o in sess.get_outputs()], sess.run(None, {inp.name: x})))
        logits = next(v for k, v in outs.items() if v.shape[-1] > 4)[0]      # (queries, classes)
        boxes = next(v for k, v in outs.items() if v.shape[-1] == 4)[0]      # (queries, 4)
        probs = 1 / (1 + np.exp(-logits))
        best_q = probs.max(axis=1).argsort()[::-1][:5]
        top = [(int(probs[q].argmax()), round(float(probs[q].max()), 3), [round(float(v), 2) for v in boxes[q]]) for q in best_q]
        print(f"  {img_name}: top (class, score, box cxcywh): {top}")
