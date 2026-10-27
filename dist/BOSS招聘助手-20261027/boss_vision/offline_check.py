"""离线验算：拿用户给的列表截图跑一遍识别+落点，产出可肉眼核对的标注图。

不需要真实屏幕，也不需要点击权限。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import vision

SRC = r"C:\Users\Administrator\.dsh\attachments\v1\objects\b0\b095403924c541bd30bffd832e6c3ccfafcd1cbd7264bd5d4c20a0b7c2d3c709"
OUT = Path(__file__).parent / "runs"


def main() -> int:
    img = np.asarray(Image.open(SRC).convert("RGB"))[:, :, ::-1].copy()
    boxes = vision.detect_teal(img)
    btns = vision.list_buttons(boxes)
    print(f"识别到「打招呼」按钮 {len(btns)} 个")

    ann = Image.fromarray(img[:, :, ::-1])
    d = ImageDraw.Draw(ann)
    for i, b in enumerate(btns, 1):
        card = vision.card_for(b)
        cx, cy = vision.click_into_resume(b)
        d.rectangle([card.x, card.y, card.x + card.w, card.y + card.h], outline=(0, 90, 255), width=3)
        d.rounded_rectangle([b.x - 3, b.y - 3, b.x + b.w + 3, b.y + b.h + 3], radius=6, outline=(255, 0, 0), width=3)
        d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], outline=(255, 0, 0), width=4)
        d.line([cx - 26, cy, cx + 26, cy], fill=(255, 0, 0), width=2)
        d.line([cx, cy - 26, cx, cy + 26], fill=(255, 0, 0), width=2)
        d.text((card.x + 12, card.y + 10), f"#{i}", fill=(255, 0, 0))
        print(f"  #{i} 卡片=({card.x},{card.y}) {card.w}x{card.h}  落点=({cx},{cy})  按钮=({b.x},{b.y}) {b.w}x{b.h}")

    OUT.mkdir(exist_ok=True)
    p = OUT / "offline-plan.png"
    ann.resize((ann.width // 2, ann.height // 2)).save(p)
    print(f"\n标注图: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
