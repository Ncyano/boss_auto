"""PaddleOCR vs qwen-vl-ocr 对照（真实简历截图）。

用法：
    python tools/bench_paddle.py            # 用当前屏幕（需先打开简历）
    python tools/bench_paddle.py --files a.png b.png
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import cdp  # noqa: E402
import vlm  # noqa: E402


def load_paddle():
    from paddleocr import PaddleOCR
    for kwargs in ({"use_angle_cls": True, "lang": "ch", "show_log": False},
                   {"lang": "ch"},
                   {}):
        try:
            return PaddleOCR(**kwargs)
        except TypeError:
            continue
    raise RuntimeError("PaddleOCR 初始化失败")


def paddle_read(ocr, img: np.ndarray) -> tuple[str, list, float]:
    """返回 (文字, 带坐标的条目, 耗时)。兼容 2.x / 3.x 两种返回结构。"""
    bgr = img
    t0 = time.time()
    res = ocr.ocr(bgr, cls=True)
    dt = time.time() - t0
    items = []

    def push(box, txt, score):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({
            "text": str(txt),
            "score": round(float(score), 3),
            "x": int(min(xs)), "y": int(min(ys)),
            "w": int(max(xs) - min(xs)), "h": int(max(ys) - min(ys)),
        })

    try:
        if isinstance(res, list) and res and isinstance(res[0], list):
            for line in res[0]:
                if len(line) >= 2:
                    push(line[0], line[1][0], line[1][1])
        elif isinstance(res, list) and res and isinstance(res[0], dict):
            for d in res[0].get("res", []) if isinstance(res[0].get("res"), list) else []:
                if "rec_texts" in d:
                    for box, txt, sc in zip(d.get("rec_boxes") or d.get("dt_polys") or [],
                                            d.get("rec_texts") or [],
                                            d.get("rec_scores") or []):
                        pts = box if isinstance(box[0], (list, tuple)) else \
                            [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
                        push(pts, txt, sc)
    except Exception as exc:
        print(f"    [warn] 解析 PaddleOCR 结果异常: {exc}")
    text = "\n".join(it["text"] for it in items)
    return text, items, dt


def filter_name(items: list, panel_x: int, panel_y: int) -> str:
    """按几何位置挑姓名：面板左上区域、2~4 个汉字。"""
    import re
    cands = []
    for it in items:
        t = it["text"].strip()
        if not re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", t):
            continue
        if not (panel_x - 40 <= it["x"] <= panel_x + 420):
            continue
        if not (panel_y - 60 <= it["y"] <= panel_y + 160):
            continue
        cands.append((it["y"], t))
    cands.sort()
    return cands[0][1] if cands else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="*", default=[])
    args = ap.parse_args()

    images = []
    if args.files:
        import cv2
        for f in args.files:
            img = cv2.imdecode(np.fromfile(f, dtype=np.uint8), cv2.IMREAD_COLOR)
            images.append((pathlib.Path(f).name, img))
    else:
        if not cdp.available():
            print("端口不可用，且没给 --files")
            return 1
        images.append(("当前屏幕", cdp.shared().screenshot()))

    print("初始化 PaddleOCR（首次会下载模型）…")
    ocr = load_paddle()
    print("  就绪")

    cfg = vlm.load_config()
    panel_x, panel_y = 1224, 120          # 简历面板大致起点（整屏坐标）

    print(f"\n{'图片':<16}{'方式':<10}{'耗时':>7}{'字数':>7}{'条数':>6}  姓名")
    for name, img in images:
        text_p, items, dt_p = paddle_read(ocr, img)
        nm_p = filter_name(items, panel_x, panel_y)
        print(f"{name:<16}{'paddle':<10}{dt_p:>6.2f}s{len(text_p):>7}{len(items):>6}  {nm_p or '(空)'}")

        t0 = time.time()
        r = vlm.read_screen_text(img, cfg) or {}
        dt_q = time.time() - t0
        txt_q = (r.get("text") or "")
        nm_q = vlm.clean_name(txt_q.splitlines()[0]) if txt_q else ""
        print(f"{'':<16}{'qwen':<10}{dt_q:>6.2f}s{len(txt_q):>7}{'—':>6}  {nm_q or '(空)'}")

        # 抽样看 paddle 的内容质量
        lines = [it["text"] for it in items][:6]
        print(f"{'':<16} paddle 前几行: {' | '.join(lines)[:100]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
