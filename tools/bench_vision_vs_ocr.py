"""视觉大模型 vs OCR 对照基准（用同一张简历截图跑两条链路）。

用法：
    python tools/bench_vision_vs_ocr.py                # 用 runs 里的截图
    python tools/bench_vision_vs_ocr.py --live 3       # 现场抓 3 屏

输出：每条链路的耗时、tokens、姓名、工作/教育条数。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import numpy as np
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import vlm  # noqa: E402

VISION_PROMPT = """你在读 BOSS 直聘的候选人简历弹窗截图。

【极重要】只读取中间的简历内容区。忽略并绝不要输出：
- 页面顶部导航（推荐牛人/我的客服/账号权益 等）
- 右上角登录账号名、VIP 标识
- 左侧菜单（职位管理/推荐牛人/搜索/沟通 等）
- 浏览器地址栏、标签页标题

【姓名规则】
- name 只填简历面板内「候选人姓名」那一处文字（通常紧跟年龄/学历/状态之前）。
- 如果无法确定候选人姓名（例如只看到账号名、或姓名区域被遮挡），name 必须留空字符串 ""。
- 绝不允许把页面右上角的招聘者账号名当作候选人姓名。

输出严格 JSON，不要解释、不要代码块：
{
  "name": "候选人姓名，不确定则空",
  "age": "数字或空",
  "education_mode": "全日制/非全日制/空",
  "graduation_year": "四位年份或空",
  "status": "求职状态原文或空",
  "expect": "期望职位原文或空",
  "work": [{"company": "", "title": "", "period": "", "descriptions": ["职责条目"]}],
  "education": [{"school": "", "major": "", "degree": "", "period": ""}]
}
"""


def load_shots() -> list[pathlib.Path]:
    return sorted((ROOT / "boss_vision" / "runs").glob("*.png"))[:10]


def grab_live(n: int) -> list[np.ndarray]:
    import cdp
    c = cdp.CDP()
    out = []
    for i in range(n):
        out.append(c.screenshot())
        print(f"  已抓 {i+1}/{n}")
        time.sleep(1.0)
    c.close()
    return out


def to_bgr(img) -> np.ndarray:
    if isinstance(img, np.ndarray):
        return img
    return np.array(img.convert("RGB"))[:, :, ::-1]


def run_ocr(bgr: np.ndarray, cfg: dict) -> dict:
    t0 = time.time()
    r = vlm.read_screen_text(bgr, cfg) or {}
    txt = r.get("text") or ""
    usage = r.get("usage") or {}
    t_ocr = time.time() - t0

    t1 = time.time()
    import judge
    st = judge.structure_text(txt, judge.struct_config()) or {}
    t_struct = time.time() - t1
    parts = vlm.normalize((st.get("data") or {}).get("sections"))
    return {
        "chain": "ocr",
        "seconds": round(t_ocr + t_struct, 1),
        "ocr_s": round(t_ocr, 1),
        "struct_s": round(t_struct, 1),
        "tokens": usage.get("total_tokens") or 0,
        "name": ((st.get("data") or {}).get("basic") or {}).get("name") or "",
        "work": len(parts.get("work") or []),
        "edu": len(parts.get("education") or []),
        "chars": len(txt),
    }


def run_vision(bgr: np.ndarray, cfg: dict) -> dict:
    t0 = time.time()
    r = vlm.ask_vision(VISION_PROMPT, bgr, cfg) or {}
    dt = time.time() - t0
    data = r.get("data") or {}
    return {
        "chain": "vision",
        "seconds": round(dt, 1),
        "tokens": (r.get("usage") or {}).get("total_tokens") or 0,
        "name": str(data.get("name") or "").strip(),
        "work": len(data.get("work") or []),
        "edu": len(data.get("education") or []),
        "chars": len(json.dumps(data, ensure_ascii=False)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=int, default=0)
    args = ap.parse_args()

    cfg = dict(vlm.load_config())
    cfg["max_image_side"] = int(cfg.get("max_image_side") or 1024)

    images = []
    if args.live:
        images = grab_live(args.live)
    else:
        for p in load_shots():
            images.append(to_bgr(Image.open(p)))
        print(f"  使用 {len(images)} 张已存截图")

    if not images:
        print("没有可用截图")
        return 1

    if not hasattr(vlm, "ask_vision"):
        print("vlm.ask_vision 尚未实现")
        return 2

    print(f"\n{'#':<3}{'链路':<8}{'耗时':>7}{'tokens':>8}{'工作':>5}{'教育':>5}  姓名")
    for i, img in enumerate(images, 1):
        for fn in (run_ocr, run_vision):
            try:
                r = fn(img, cfg)
            except Exception as exc:
                print(f"{i:<3}{fn.__name__:<8}  失败: {exc}")
                continue
            print(f"{i:<3}{r['chain']:<8}{r['seconds']:>6}s{r['tokens']:>8}"
                  f"{r['work']:>5}{r['edu']:>5}  {r['name'] or '(空)'}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
