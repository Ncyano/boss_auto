"""视觉模型对比测试：同一批截图，跑多个模型，比准确率 / 耗时 / 成本。

用法：
    # 用已保存的截图（需复核的候选人截图在 runs\\<stamp>-shots\\）
    python tools\\bench_vlm.py --images boss_vision\\runs\\20261021-152929-shots

    # 指定多个模型（name=base_url|model，key 从环境变量读）
    python tools\\bench_vlm.py --images xxx ^
        --model "qwen-flash=https://dashscope.aliyuncs.com/compatible-mode/v1|qwen3-vl-flash" ^
        --model "qwen-plus=https://dashscope.aliyuncs.com/compatible-mode/v1|qwen3-vl-plus" ^
        --model "glm=https://open.bigmodel.cn/api/paas/v4|glm-4.6v-flash"

输出：每个模型的 JSON 合法率、条目数、耗时、token 用量，并把结果存成 json 便于逐字对比。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import vlm  # noqa: E402


def load_images(folder: Path, limit: int = 0) -> list[Path]:
    files = sorted(p for p in folder.glob("*.png"))
    if limit:
        files = files[:limit]
    return files


def parse_model_arg(text: str) -> dict:
    """name=base_url|model"""
    name, _, rest = text.partition("=")
    base_url, _, model = rest.partition("|")
    if not (name and base_url and model):
        raise SystemExit(f"--model 格式应为 name=base_url|model，收到：{text}")
    return {"name": name.strip(), "base_url": base_url.strip(), "model": model.strip()}


def run_model(cfg: dict, images: list[Path], out_dir: Path) -> dict:
    from PIL import Image
    import numpy as np

    stats = {"model": cfg["model"], "ok": 0, "fail": 0, "entries": 0, "education": 0,
             "images": 0, "seconds": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
             "errors": []}
    out_dir.mkdir(parents=True, exist_ok=True)

    for path in images:
        bgr = np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1].copy()
        t0 = time.time()
        try:
            raw = vlm.read_screen(bgr, cfg, log=lambda *_: None)
        except Exception as exc:
            stats["fail"] += 1
            stats["errors"].append(f"{path.name}: {exc}")
            continue
        stats["seconds"] += time.time() - t0
        stats["images"] += 1
        stats["ok"] += 1
        parts = vlm.normalize(raw["sections"])
        stats["entries"] += len(parts["work"]) + len(parts["projects"]) + len(parts["campus"])
        stats["education"] += len(parts["education"])
        usage = raw.get("usage") or {}
        stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        (out_dir / f"{path.stem}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    if stats["images"]:
        stats["avg_seconds"] = round(stats["seconds"] / stats["images"], 2)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True, help="截图目录")
    ap.add_argument("--model", action="append", default=[],
                    help="name=base_url|model，可重复；不填则用 vlm-config.json")
    ap.add_argument("--limit", type=int, default=0, help="每个模型最多测几张")
    ap.add_argument("--out", default=None, help="结果输出目录")
    args = ap.parse_args(argv)

    images = load_images(Path(args.images), args.limit)
    if not images:
        print(f"{args.images} 里没有 png 截图")
        return 2
    print(f"共 {len(images)} 张截图\n")

    base = vlm.load_config()
    models = ([parse_model_arg(m) for m in args.model] if args.model
              else [{"name": "default", "base_url": base["base_url"], "model": base["model"]}])

    out_root = Path(args.out) if args.out else ROOT / "boss_vision" / "runs" / "bench"
    results = []
    for spec in models:
        cfg = dict(base)
        cfg.update({"base_url": spec["base_url"], "model": spec["model"]})
        print(f"=== {spec['name']} ({spec['model']}) ===")
        stats = run_model(cfg, images, out_root / spec["name"])
        stats["name"] = spec["name"]
        results.append(stats)
        if stats.get("errors"):
            for err in stats["errors"][:3]:
                print(f"   ✗ {err[:110]}")
        if stats["images"]:
            print(f"   成功 {stats['ok']} 张 / 失败 {stats['fail']} 张，"
                  f"平均 {stats.get('avg_seconds')}s，"
                  f"token {stats['prompt_tokens']}+{stats['completion_tokens']}，"
                  f"条目 {stats['entries']} 段")
        print()

    print("=== 汇总 ===")
    print(f"{'模型':<14}{'成功':>5}{'失败':>5}{'条目':>6}{'平均秒':>8}{'输入token':>10}{'输出token':>10}")
    for r in results:
        print(f"{r['name']:<14}{r['ok']:>5}{r['fail']:>5}{r['entries']:>6}"
              f"{r.get('avg_seconds', 0):>8}{r['prompt_tokens']:>10}{r['completion_tokens']:>10}")

    (out_root / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n每张图的原始输出在 {out_root}\\<模型名>\\<图名>.json，可逐字对比")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
