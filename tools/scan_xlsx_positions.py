"""把 Excel 里嵌的简历截图 OCR 出来，统计「期望职位」分布。

用法：
    .venv\\Scripts\\python.exe tools\\scan_xlsx_positions.py "未命名表格 (2).xlsx"
    .venv\\Scripts\\python.exe tools\\scan_xlsx_positions.py "未命名表格 (2).xlsx" --limit 60 --workers 4

输出：
    控制台统计 + boss_vision\\runs\\xlsx-positions.json（逐张明细，可续跑）
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))

import settings  # noqa: E402
import vlm  # noqa: E402

# 期望职位 后面常跟 "城市 | 岗位 | 行业 | 薪资"
POS = re.compile(r"期望职位\s*[：:]?\s*([^\n]{2,60})")
EXP = re.compile(r"(福州|厦门|泉州|漳州|莆田|宁德|三明|南平|龙岩|温州|上海|北京|深圳|广州)\s*[|｜]\s*([^|｜\n]{2,20})")


def extract(text: str) -> dict:
    t = text.replace("\r", "")
    out = {"raw_pos": "", "city": "", "position": ""}
    m = POS.search(t)
    if m:
        out["raw_pos"] = m.group(1).strip()[:60]
        m2 = EXP.search(out["raw_pos"])
        if m2:
            out["city"], out["position"] = m2.group(1), m2.group(2).strip()
        else:
            out["position"] = out["raw_pos"]
    else:
        m3 = EXP.search(t)
        if m3:
            out["city"], out["position"] = m3.group(1), m3.group(2).strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张（0=全部）")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    path = REPO / args.xlsx
    z = zipfile.ZipFile(path)
    media = sorted([n for n in z.namelist() if "/media/" in n])
    if args.limit:
        media = media[: args.limit]
    print(f"  图片总数: {len(media)}  并发: {args.workers}")

    cfg = dict(settings.load().get("vision") or {})
    cfg["max_image_side"] = int(cfg.get("max_image_side") or 1400)

    def work(name: str) -> dict:
        try:
            im = Image.open(io.BytesIO(z.read(name))).convert("RGB")
            arr = np.array(im)[:, :, ::-1]
            r = vlm.read_screen_text(arr, cfg)
            txt = ((r or {}).get("text") or "")
            info = extract(txt)
            info["image"] = name
            info["chars"] = len(txt)
            return info
        except Exception as exc:
            return {"image": name, "error": f"{type(exc).__name__}: {exc}"}

    rows: list[dict] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, n): n for n in media}
        for i, f in enumerate(as_completed(futs), 1):
            rows.append(f.result())
            if i % 10 == 0 or i == len(media):
                print(f"    {i}/{len(media)}  {time.time() - t0:.0f}s")

    ok = [r for r in rows if not r.get("error")]
    pos = Counter(r.get("position") or "(未识别)" for r in ok)
    city = Counter(r.get("city") or "(未识别)" for r in ok)

    print("\n  === 期望职位分布 ===")
    for p, n in pos.most_common(40):
        print(f"    {n:4d}  {p[:40]}")
    print("\n  === 城市分布 ===")
    for c, n in city.most_common(12):
        print(f"    {n:4d}  {c}")
    err = [r for r in rows if r.get("error")]
    if err:
        print(f"\n  失败 {len(err)} 张：{err[0].get('error')}")

    out = REPO / "boss_vision" / "runs"
    out.mkdir(parents=True, exist_ok=True)
    dst = out / "xlsx-positions.json"
    dst.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  明细: {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
