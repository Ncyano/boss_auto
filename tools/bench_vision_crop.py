"""P0 提速验证：裁剪简历面板 + 1280px + 紧凑提示词，对比整屏。

用法：
    python tools/bench_vision_crop.py --live 3
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import vlm  # noqa: E402

FULL_PROMPT = """你在读 BOSS 直聘的候选人简历弹窗截图。
只读取中间的简历内容区，忽略顶部导航、右上角账号名、左侧菜单、地址栏。
姓名只取简历面板内候选人姓名，无法确定则留空 ""。
输出 JSON：{"name":"","age":"","education_mode":"","graduation_year":"","status":"",
"expect":"","work":[{"company":"","title":"","period":"","descriptions":[]}],
"education":[{"school":"","major":"","degree":"","period":""}]}
只输出 JSON。"""

CROP_PROMPT = """这是 BOSS 直聘简历面板的截图。
输出 JSON：{"name":"","age":"","education_mode":"","graduation_year":"","status":"",
"expect":"","work":[{"company":"","title":"","period":""}],
"education":[{"school":"","major":"","degree":"","period":""}]}
姓名字段只填候选人姓名，找不到就留空。只输出 JSON。"""


def crop_panel(bgr: np.ndarray) -> np.ndarray:
    """按 BOSS 布局裁掉左侧菜单与顶部导航，只留简历面板。

    实测布局：左侧菜单约 8% 宽，顶部导航约 8% 高；
    简历弹窗大致落在 x>40%、y>6% 的区域，向右留到页面边缘。
    """
    h, w = bgr.shape[:2]
    x0 = int(w * 0.40)
    y0 = int(h * 0.06)
    return bgr[y0:h, x0:w]


def align(img: np.ndarray, max_side: int) -> np.ndarray:
    """等比缩放到长边 max_side。"""
    h, w = img.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale >= 1.0:
        return img
    import cv2
    return cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def run(prompt: str, img: np.ndarray, cfg: dict) -> dict:
    t0 = time.time()
    r = vlm.ask_vision(prompt, img, cfg)
    dt = time.time() - t0
    data = r.get("data") or {}
    return {
        "s": round(dt, 1),
        "tok": (r.get("usage") or {}).get("total_tokens") or 0,
        "name": str(data.get("name") or ""),
        "work": len(data.get("work") or []),
        "edu": len(data.get("education") or []),
        "px": f"{img.shape[1]}x{img.shape[0]}",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=int, default=2)
    args = ap.parse_args()

    import cdp
    c = cdp.CDP()
    shots = []
    for i in range(args.live):
        shots.append(c.screenshot())
        print(f"  抓屏 {i+1}/{args.live}")
        time.sleep(0.8)
    c.close()

    base = dict(vlm.load_config())
    full_cfg = dict(base, max_image_side=1280, max_tokens=1600)
    crop_cfg = dict(base, max_image_side=900, max_tokens=1600)

    print(f"\n{'#':<3}{'方式':<10}{'尺寸':>12}{'耗时':>7}{'tokens':>8}{'工作':>5}{'教育':>5}  姓名")
    for i, bgr in enumerate(shots, 1):
        panel = align(crop_panel(bgr), 900)
        full = align(bgr, 1280)
        for label, prompt, img, cfg in (
            ("整屏1280", FULL_PROMPT, full, full_cfg),
            ("裁剪+强提示", FULL_PROMPT, panel, crop_cfg),
        ):
            try:
                r = run(prompt, img, cfg)
            except Exception as exc:
                print(f"{i:<3}{label:<10} 失败: {exc}")
                continue
            print(f"{i:<3}{label:<10}{r['px']:>12}{r['s']:>6}s{r['tok']:>8}"
                  f"{r['work']:>5}{r['edu']:>5}  {r['name'] or '(空)'}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
