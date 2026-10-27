"""对比两个视觉模型在同一张截图上的耗时与识别质量。

用法：
    # 用当前 BOSS 页面截图对比（需要调试端口）
    .venv\\Scripts\\python.exe tools\\bench_ocr_models.py --grab

    # 用已有图片对比
    .venv\\Scripts\\python.exe tools\\bench_ocr_models.py --image data\\sample\\resume1.png

    # 只用某一个模型
    .venv\\Scripts\\python.exe tools\\bench_ocr_models.py --grab --only deepseek

输出：耗时、字数、两模型文本的字符级相似度、各自前几行。
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))

import numpy as np  # noqa: E402

import vlm  # noqa: E402


def load_qwen_cfg() -> dict:
    cfg = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
    return dict(cfg["vision"])


def deepseek_cfg(base: dict) -> dict:
    """基于同一份配置，改成 deepseek 视觉。"""
    judge = json.loads((REPO / "config.json").read_text(encoding="utf-8"))["judge"]
    cfg = dict(base)
    cfg.update({
        "provider": "deepseek",
        "base_url": judge["base_url"],
        "model": "deepseek-flash",
        "api_key": judge["api_key"],
        "api_key_env": judge.get("api_key_env", "DEEPSEEK_API_KEY"),
        "max_tokens": 2000,
        "ocr_max_tokens": 2000,
    })
    return cfg


def grab_screen() -> np.ndarray:
    import cdp
    import winenv
    if not cdp.available():
        raise SystemExit("调试端口没开：先运行 run-edge-debug.cmd")
    winenv.set_capture("cdp")
    return winenv.grab_window((0, 0, 0, 0))


def run_one(name: str, img: np.ndarray, cfg: dict) -> dict:
    print(f"\n=== {name}（model={vlm.ocr_model(cfg)}）===")
    t0 = time.time()
    try:
        out = vlm.read_screen_text(img, cfg, log=lambda *a, **k: None)
        text = (out or {}).get("text", "")
    except Exception as exc:
        print(f"  [失败] {type(exc).__name__}: {exc}")
        return {"name": name, "sec": time.time() - t0, "text": "", "error": str(exc)}
    dt = time.time() - t0
    print(f"  耗时: {dt:.2f} 秒 | 字数: {len(text)}")
    head = [ln for ln in text.splitlines() if ln.strip()][:6]
    for ln in head:
        print(f"    {ln[:60]}")
    return {"name": name, "sec": dt, "text": text}


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="")
    ap.add_argument("--grab", action="store_true")
    ap.add_argument("--only", default="", choices=["", "qwen", "deepseek"])
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    if args.image:
        import cv2
        img = cv2.imdecode(np.fromfile(args.image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise SystemExit(f"读不到图片：{args.image}")
    elif args.grab:
        img = grab_screen()
    else:
        raise SystemExit("要么 --grab，要么 --image <路径>")

    print(f"  图片尺寸: {img.shape[1]}x{img.shape[0]}")
    if args.save:
        import cv2
        cv2.imwrite(args.save, img)
        print(f"  已保存: {args.save}")

    base = load_qwen_cfg()
    results = []
    if args.only in ("", "qwen"):
        results.append(run_one("qwen-vl-ocr", img, base))
    if args.only in ("", "deepseek"):
        results.append(run_one("deepseek-flash", img, deepseek_cfg(base)))

    if len(results) == 2:
        a, b = results
        print("\n=== 对比 ===")
        print(f"  qwen-vl-ocr    {a['sec']:.2f}s  {len(a['text'])} 字")
        print(f"  deepseek-flash {b['sec']:.2f}s  {len(b['text'])} 字")
        print(f"  文本相似度: {similarity(a['text'], b['text'])}")
        if a["text"] and b["text"]:
            only_a = set(a["text"]) - set(b["text"])
            only_b = set(b["text"]) - set(a["text"])
            print(f"  qwen 独有字符: {''.join(sorted(only_a))[:40] or '无'}")
            print(f"  deepseek 独有字符: {''.join(sorted(only_b))[:40] or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
