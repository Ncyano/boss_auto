"""用用户提供的三张截图做像素级校准：找按钮、找卡片、量弹窗布局。

只读分析，不碰屏幕、不点任何东西。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "calib"
OUT.mkdir(exist_ok=True)

IMAGES = {
    "list": r"C:\Users\Administrator\.dsh\attachments\v1\objects\b0\b095403924c541bd30bffd832e6c3ccfafcd1cbd7264bd5d4c20a0b7c2d3c709",
    "list_anno": r"C:\Users\Administrator\.dsh\attachments\v1\objects\5d\5dd2b004f1bec23853a9b66f9deb5527db93ec58fbac5a077d5236f892baff63",
    "resume": r"C:\Users\Administrator\.dsh\attachments\v1\objects\86\86fa691474abc10c5a4c6b978b5746d449d1f720f1d590e1751a5455aec1d853",
}


def runs(mask: np.ndarray, min_len: int):
    """一维布尔数组里长度 >= min_len 的连续 True 区间 [(start, end_inclusive)]。"""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]]
    return [(int(s), int(e)) for s, e in zip(starts, ends) if e - s + 1 >= min_len]


def find_teal(img: np.ndarray, min_area=400):
    """BOSS 的青绿色按钮：r 很低，g/b 高。返回连通块的近似 bbox 列表。"""
    r, g, b = img[:, :, 0].astype(int), img[:, :, 1].astype(int), img[:, :, 2].astype(int)
    mask = (r < 110) & (g > 140) & (b > 130) & (g - r > 60) & (b - r > 50)
    boxes = []
    for y0, y1 in runs(mask.any(axis=1), 8):
        band = mask[y0 : y1 + 1]
        for x0, x1 in runs(band.any(axis=0), 8):
            sub = mask[y0 : y1 + 1, x0 : x1 + 1]
            if sub.sum() >= min_area:
                boxes.append(
                    {
                        "x": int(x0),
                        "y": int(y0),
                        "w": int(x1 - x0 + 1),
                        "h": int(y1 - y0 + 1),
                        "fill": round(float(sub.mean()), 3),
                        "cx": int((x0 + x1) / 2),
                        "cy": int((y0 + y1) / 2),
                    }
                )
    return boxes


def find_cards(img: np.ndarray, x0: int, x1: int):
    """在列表列范围内，按“接近白色”的行来切分卡片。"""
    strip = img[:, x0:x1, :].mean(axis=(1, 2))
    white = strip > 250  # 卡片 #fff，页面底色偏灰
    out = []
    for s, e in runs(white, 60):
        if e - s + 1 > 800:  # 整块白色背景，不是卡片
            continue
        out.append({"y": s, "h": e - s + 1, "cy": int((s + e) / 2)})
    return out


def main() -> int:
    report = {}
    for name, path in IMAGES.items():
        im = Image.open(path).convert("RGB")
        arr = np.asarray(im)
        h, w = arr.shape[:2]
        teal = find_teal(arr)
        entry = {"size": [w, h], "teal_blocks": teal}
        if name.startswith("list"):
            entry["cards"] = find_cards(arr, int(w * 0.29), int(w * 0.78))
        report[name] = entry

        # 画标注图，肉眼确认识别结果
        ann = im.copy()
        d = ImageDraw.Draw(ann)
        for t in teal:
            d.rectangle([t["x"], t["y"], t["x"] + t["w"], t["y"] + t["h"]], outline=(255, 0, 0), width=3)
        for c in entry.get("cards", []):
            d.rectangle([int(w * 0.29), c["y"], int(w * 0.78), c["y"] + c["h"]], outline=(0, 0, 255), width=2)
        ann.save(OUT / f"{name}_annotated.png")

        # 保存按钮模板，供后续 matchTemplate 使用
        big = [t for t in teal if t["w"] > 50 and t["h"] > 20]
        for i, t in enumerate(big[:3]):
            im.crop((t["x"], t["y"], t["x"] + t["w"], t["y"] + t["h"])).save(OUT / f"teal_{name}_{i}.png")

    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, e in report.items():
        print(f"== {name} {e['size'][0]}x{e['size'][1]} ==")
        print(f"  teal blocks: {len(e['teal_blocks'])}")
        for t in e["teal_blocks"][:12]:
            print(f"    x={t['x']:>5} y={t['y']:>5} {t['w']:>4}x{t['h']:<4} fill={t['fill']} center=({t['cx']},{t['cy']})")
        if "cards" in e:
            print(f"  cards: {len(e['cards'])}")
            for c in e["cards"]:
                print(f"    y={c['y']:>5} h={c['h']:>4} cy={c['cy']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
