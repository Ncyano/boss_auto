"""姓名识别回归测试（真实调用视觉模型，需要 VLM_API_KEY）。

    python boss_vision\\test_name.py

没有 key 时自动跳过，不会失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import numpy as np  # noqa: E402
import vision  # noqa: E402
import vlm  # noqa: E402
from PIL import Image  # noqa: E402

FIX = _HERE / "tests" / "fixtures"
PASS = 0
FAIL = 0
SKIP = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    global SKIP
    cfg = vlm.load_config()
    if not vlm.api_key(cfg):
        print("[skip] 没有配置 VLM_API_KEY，跳过真实调用测试")
        return 0

    band = vision.Rect(800, 20, 780, 1260)

    print("[1] 姓名清洗（不调模型）")
    for raw, want in [("王伟俐 3日内活跃", "王伟俐"), ("张锦敏刚刚活跃", "张锦敏"),
                      ("空字符串", ""), ('"李四"', "李四"), ("26岁 大专", "")]:
        got = vlm.clean_name(raw)
        check(f"{raw!r} -> {want!r}", got == want, f"得到 {got!r}")

    print("\n[2] 真实截图：面板里的姓名")
    panel = FIX / "panel-zhangjinmin.png"
    if panel.exists():
        img = np.asarray(Image.open(panel).convert("RGB"))[:, :, ::-1].copy()
        name, votes = vlm.read_name_consensus(img, band, cfg, log=lambda *_: None)
        check("张锦敏 读对", name == "张锦敏", f"{name!r} 票={votes}")
    else:
        print("  [skip] 缺少 panel-zhangjinmin.png")

    print("\n[3] 真实截图：容易被认错的姓名（王伟俐）")
    crop_path = FIX / "name-wangweili.png"
    if crop_path.exists():
        crop = np.asarray(Image.open(crop_path).convert("RGB"))[:, :, ::-1].copy()
        canvas = np.full((1400, 2560, 3), 255, dtype=np.uint8)
        canvas[180: 180 + crop.shape[0], 800: 800 + crop.shape[1]] = crop
        name, votes = vlm.read_name_consensus(canvas, band, cfg, log=lambda *_: None)
        check("王伟俐 读对（整屏容易读成王伟刚）", name == "王伟俐", f"{name!r} 票={votes}")
    else:
        print("  [skip] 缺少 name-wangweili.png")

    print(f"\n{PASS}/{PASS + FAIL} 通过" + (f"，跳过 {SKIP}" if SKIP else ""))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
