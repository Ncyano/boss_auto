"""把一个候选人的多张截图用视觉模型读完，和 CSV 里的 OCR 结果并排对比。

     python data/_vlm_compare.py 01 05 10 21
"""
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "boss_vision"))

import numpy as np  # noqa: E402
import vlm  # noqa: E402
from PIL import Image  # noqa: E402

SHOTS = ROOT / "boss_vision" / "runs" / "20261021-152929-shots"
CSV_PATH = ROOT / "boss_vision" / "runs" / "2026-10-21.csv"


def ocr_row(index: str) -> dict:
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["序号"].zfill(2) == index.zfill(2):
                return row
    return {}


def main(indices: list[str]) -> int:
    cfg = vlm.load_config()
    for idx in indices:
        shots = sorted(SHOTS.glob(f"{idx}-*.png"))
        if not shots:
            print(f"#{idx} 没有截图")
            continue

        work, education = [], []
        t0 = time.time()
        tokens = 0
        for shot in shots:
            bgr = np.asarray(Image.open(shot).convert("RGB"))[:, :, ::-1].copy()
            raw = vlm.read_screen(bgr, cfg, log=lambda *_: None)
            parts = vlm.normalize(raw["sections"])
            vlm.merge_screens(work, parts["work"])
            for e in parts["education"]:
                if not any(e["period"] == x["period"] and e["school"] == x["school"] for x in education):
                    education.append(e)
            tokens += int((raw.get("usage") or {}).get("total_tokens") or 0)

        row = ocr_row(idx)
        print("=" * 100)
        print(f"#{idx} {row.get('姓名', '')}   VLM: {len(shots)} 屏 {time.time()-t0:.0f}s {tokens} tokens")
        print(f"  OCR 结果：{row.get('工作经历条数')} 段 | 学校={row.get('学校')} 学历={row.get('学历')}")
        print("  VLM 结果：")
        for w in work:
            print(f"    [{w['period'] or '—':<18}] {w['company'] or '(缺公司)'} / {w['title'] or '(缺职位)'}"
                  f"  职责{len(w['descriptions'])} 标签{len(w['tags'])}")
        for e in education:
            print(f"    [教育] {e['school']} / {e['major']} / {e['degree']} / {e['period']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["01"]))
