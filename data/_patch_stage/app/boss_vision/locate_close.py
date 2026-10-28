"""验算：从「打招呼」大按钮锚点推出 ✕ 关闭按钮的位置，并在截图上画出来核对。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import vision

SRC = r"C:\Users\Administrator\.dsh\attachments\v1\objects\d9\d9f6912234fb872fa095eebf40fd994f2b31c22bd74ffa437771d268768e9e72"
OUT = _HERE / "runs"


def main() -> int:
    im = Image.open(SRC).convert("RGB")
    img = np.asarray(im)[:, :, ::-1].copy()

    boxes = vision.detect_teal(img)
    wides = vision.wide_buttons(boxes)
    print(f"大按钮 {len(wides)} 个: {[(w.x, w.y, w.w, w.h) for w in wides]}")
    if not wides:
        return 1

    cx, cy = vision.close_button(wides[0])
    print(f"推算出的关闭 [X] 位置: ({cx}, {cy})")

    ann = im.copy()
    d = ImageDraw.Draw(ann)
    w = wides[0]
    d.rectangle([w.x, w.y, w.x + w.w, w.y + w.h], outline=(0, 90, 255), width=3)
    d.rectangle([cx - 14, cy - 14, cx + 14, cy + 14], outline=(255, 0, 0), width=3)
    d.line([cx - 26, cy, cx + 26, cy], fill=(255, 0, 0), width=2)
    d.line([cx, cy - 26, cx, cy + 26], fill=(255, 0, 0), width=2)
    d.line([w.x + w.w, w.y, cx, cy], fill=(0, 150, 0), width=2)

    OUT.mkdir(exist_ok=True)
    ann.crop((1500, 80, 2050, 400)).resize((1100, 640), Image.LANCZOS).save(OUT / "close-anchor-check.png")
    print(f"核对图: {OUT / 'close-anchor-check.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
