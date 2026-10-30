"""Export RF-DETR (Apache-2.0) nano/small/medium to ONNX for the Lookout hub.

Writes rfdetr-{size}.onnx next to this script, plus rfdetr.json describing each model's input
size and the person class index (read from the model, not assumed).
Run: .venv/Scripts/python export_rfdetr.py
"""

import json
import shutil
from pathlib import Path

import rfdetr

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
info = {}

for size, cls in (("nano", rfdetr.RFDETRNano), ("small", rfdetr.RFDETRSmall), ("medium", rfdetr.RFDETRMedium)):
    model = cls()                       # downloads the COCO checkpoint on first use
    res = model.model.resolution
    names = model.class_names           # {id: name} or list, depending on version
    items = names.items() if isinstance(names, dict) else enumerate(names)
    person = next(int(k) for k, v in items if str(v).lower() == "person")
    work = OUT / size
    work.mkdir(exist_ok=True)
    onnx = Path(model.export(output_dir=str(work), opset_version=17, verbose=False))
    if onnx.is_dir():
        onnx = next(onnx.glob("*.onnx"))
    dest = HERE / f"rfdetr-{size}.onnx"
    shutil.copy(onnx, dest)
    info[size] = {"file": dest.name, "resolution": res, "person_class": person,
                  "bytes": dest.stat().st_size}
    print(size, info[size], flush=True)

(HERE / "rfdetr.json").write_text(json.dumps(info, indent=2))
