"""第二遍校准：卡片矩形、简历弹窗边界、系统 DPI。"""
from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "calib"
OUT.mkdir(exist_ok=True)

LIST = r"C:\Users\Administrator\.dsh\attachments\v1\objects\b0\b095403924c541bd30bffd832e6c3ccfafcd1cbd7264bd5d4c20a0b7c2d3c709"
RESUME = r"C:\Users\Administrator\.dsh\attachments\v1\objects\86\86fa691474abc10c5a4c6b978b5746d449d1f720f1d590e1751a5455aec1d853"


def dpi() -> int:
    try:
        return int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        return -1


def runs(mask, min_len):
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    br = np.flatnonzero(np.diff(idx) > 1)
    s = np.r_[idx[0], idx[br + 1]]
    e = np.r_[idx[br], idx[-1]]
    return [(int(a), int(b)) for a, b in zip(s, e) if b - a + 1 >= min_len]


def card_rects(arr: np.ndarray):
    """列表列内，按每行“接近纯白占比”切卡片。"""
    h, w = arr.shape[:2]
    x0, x1 = 760, 1830  # 卡片内部（避开左侧头像区与右侧按钮）
    lum = arr[:, x0:x1, :].mean(axis=2)
    white_frac = (lum > 250).mean(axis=1)
    mask = white_frac > 0.55
    rects = []
    for s, e in runs(mask, 80):
        if e - s + 1 > 500:
            continue
        # 用该行段内“卡片实际左右边界”修正 x
        band = arr[s : e + 1].mean(axis=0).mean(axis=1)
        cols = np.flatnonzero(band > 250)
        left = int(cols.min()) if cols.size else 620
        right = int(cols.max()) if cols.size else 1897
        rects.append({"x": left, "y": s, "w": right - left + 1, "h": e - s + 1,
                      "cx": (left + right) // 2, "cy": (s + e) // 2})
    return rects


def panel_bounds(arr: np.ndarray):
    """简历弹窗：中间那块大面积白色区域。"""
    lum = arr.mean(axis=2)
    white = lum > 248
    col = white.mean(axis=0)
    row = white.mean(axis=1)
    xs = runs(col > 0.25, 200)
    ys = runs(row > 0.25, 200)
    if not xs or not ys:
        return None
    x0, x1 = xs[0]
    y0, y1 = ys[0]
    return {"x": int(x0), "y": int(y0), "w": int(x1 - x0 + 1), "h": int(y1 - y0 + 1)}


def main() -> int:
    print(f"GetDpiForSystem = {dpi()}  (96 = 100% 缩放)")
    out = {"dpi": dpi()}

    lst = np.asarray(Image.open(LIST).convert("RGB"))
    rects = card_rects(lst)
    out["cards"] = rects
    print(f"cards: {len(rects)}")
    for i, r in enumerate(rects):
        print(f"  #{i + 1} x={r['x']} y={r['y']:>4} {r['w']}x{r['h']}  center=({r['cx']},{r['cy']})")

    res = np.asarray(Image.open(RESUME).convert("RGB"))
    panel = panel_bounds(res)
    out["resume_panel"] = panel
    print(f"resume panel: {panel}")

    # 标注图（缩小到 1/2 便于查看）
    for tag, arr, extra in (("cards", lst, rects),):
        im = Image.fromarray(arr)
        d = ImageDraw.Draw(im)
        for i, r in enumerate(extra):
            d.rectangle([r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]], outline=(255, 0, 0), width=4)
            d.text((r["x"] + 8, r["y"] + 6), f"#{i + 1}", fill=(255, 0, 0))
        im.resize((im.width // 2, im.height // 2)).save(OUT / "cards_check.png")

    im = Image.fromarray(res)
    d = ImageDraw.Draw(im)
    if panel:
        d.rectangle([panel["x"], panel["y"], panel["x"] + panel["w"], panel["y"] + panel["h"]],
                    outline=(255, 0, 0), width=4)
    d.rectangle([1609, 252, 1894, 292], outline=(0, 128, 255), width=4)  # 弹窗里的打招呼
    im.resize((im.width // 2, im.height // 2)).save(OUT / "panel_check.png")

    (OUT / "report2.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
