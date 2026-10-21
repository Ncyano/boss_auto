"""标定「人像图标」模板，并验证模板匹配能把它和旁边的饼图区分开。

      python calibrate_icon.py --image tests/fixtures/icons_src.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import vision

ASSETS = _HERE / "assets"
ASSETS.mkdir(exist_ok=True)
TEMPLATE = ASSETS / "icon_person.png"


def teal_mask(sub: np.ndarray) -> np.ndarray:
    b, g, r = (sub[:, :, i].astype(int) for i in range(3))
    return ((g > 110) & (b > 110) & (r < 150) & ((g - r) > 20)).astype(np.uint8)


def icon_blobs(strip: np.ndarray) -> list[tuple[int, int, int, int]]:
    """在一条图标带里找出各个青色图标的外接框（按 x 排序）。

    同一个图标的笔画可能被拆成几块（例如人像的"肩线"是一条独立的短线），
    这里把水平相邻且垂直重叠的块合并成一个图标。
    """
    mask = teal_mask(strip)
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
    raw = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 20:
            continue
        raw.append([int(x), int(y), int(w), int(h)])
    raw.sort(key=lambda b: b[0])

    merged: list[list[int]] = []
    for x, y, w, h in raw:
        if merged:
            mx, my, mw, mh = merged[-1]
            v_overlap = min(my + mh, y + h) - max(my, y)
            if x <= mx + mw + 3 and v_overlap >= min(mh, h) * 0.5:
                merged[-1] = [mx, min(my, y), max(mx + mw, x + w) - mx, max(my + mh, y + h) - min(my, y)]
                continue
        merged.append([x, y, w, h])
    return [tuple(b) for b in merged]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--write", action="store_true", help="用这张图里的图标生成模板")
    a = ap.parse_args()

    img = np.asarray(Image.open(a.image).convert("RGB"))[:, :, ::-1].copy()
    btns = vision.list_buttons(vision.detect_teal(img))
    print(f"卡片 {len(btns)} 张")

    rows = []
    for i, b in enumerate(btns, 1):
        x0, x1 = b.x - 112, b.x - 2
        y0, y1 = b.cy - 24, b.cy + 24
        strip = img[y0:y1, x0:x1].copy()
        blobs = icon_blobs(strip)
        rows.append((i, b, strip, blobs, (x0, y0)))
        desc = ", ".join(f"x{x0 + bx}({bw}x{bh})" for bx, by, bw, bh in blobs)
        print(f"  #{i} 图标 {len(blobs)} 个: {desc}")

    if a.write:
        # 取图标数最多的那张卡的「最右」图标当模板（人像图标总是最靠右）
        best = max(rows, key=lambda r: len(r[3]))
        i, b, strip, blobs, (sx, sy) = best
        bx, by, bw, bh = blobs[-1]
        tmpl = strip[max(0, by - 2): by + bh + 2, max(0, bx - 2): bx + bw + 2]
        Image.fromarray(tmpl[:, :, ::-1]).save(TEMPLATE)
        print(f"\n用卡片 #{i} 的最右图标生成模板: {TEMPLATE}  尺寸 {tmpl.shape[1]}x{tmpl.shape[0]}")

    tmpl = cv2.imread(str(TEMPLATE))
    if tmpl is None:
        print("还没有模板，先加 --write")
        return 1
    print("\n模板匹配得分（1.0 = 完全一致）：")
    for i, b, strip, blobs, _ in rows:
        res = cv2.matchTemplate(strip, tmpl, cv2.TM_CCOEFF_NORMED)
        score = float(res.max())
        print(f"  #{i} 最右图标数={len(blobs)}  匹配得分={score:.3f}  "
              f"{'有人像图标' if score >= 0.8 else '没有人像图标'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
