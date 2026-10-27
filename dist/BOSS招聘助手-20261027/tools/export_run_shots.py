"""把一次采集的逐屏截图按候选人导出，并在文件名前标上判定结论。

用法：
  python tools/export_run_shots.py                      # 导出 runs 下最新一批
  python tools/export_run_shots.py --run 20261023-160115
  python tools/export_run_shots.py --out D:\\导出

输出结构：
  <out>/合适/03_张三_3屏/01.png, 02.png …
  <out>/不合适/07_李四_2屏/01.png …
  <out>/索引.csv    候选人、结论、原因、截图数
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "boss_vision" / "runs"


def latest_run() -> str:
    files = sorted(RUNS.glob("*-results.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit("runs 下没有 results.json")
    return files[0].name.replace("-results.json", "")


def safe(text: str, limit: int = 20) -> str:
    t = re.sub(r'[\\/:*?"<>|\s]+', "_", (text or "").strip())
    return t[:limit] or "无名"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="", help="批次号，如 20261023-160115")
    ap.add_argument("--out", default="导出简历图")
    args = ap.parse_args()

    run = args.run or latest_run()
    json_path = RUNS / f"{run}-results.json"
    if not json_path.exists():
        raise SystemExit(f"找不到 {json_path}")
    records = json.load(open(json_path, encoding="utf-8"))
    shot_dir = RUNS / f"{run}-shots"
    out_dir = Path(args.out) / run

    rows, exported, missing = [], 0, 0
    for rec in records:
        idx = rec.get("index") or 0
        name = safe(rec.get("name") or rec.get("card_name") or "")
        verdict = ((rec.get("judgment") or {}).get("verdict") or "").strip() or "未判定"
        reason = (rec.get("judgment") or {}).get("reason") or ""
        stage = (rec.get("judgment") or {}).get("stage") or ""

        shots = sorted(shot_dir.glob(f"{idx:02d}-*.png")) if shot_dir.exists() else []
        if not shots:
            missing += 1
            rows.append({"序号": idx, "姓名": name, "结论": verdict, "环节": stage,
                         "原因": reason[:120], "截图数": 0})
            continue

        folder = out_dir / verdict / f"{idx:02d}_{name}_{len(shots)}屏"
        folder.mkdir(parents=True, exist_ok=True)
        for k, src in enumerate(shots, 1):
            shutil.copy2(src, folder / f"{k:02d}{src.suffix}")
        exported += 1
        rows.append({"序号": idx, "姓名": name, "结论": verdict, "环节": stage,
                     "原因": reason[:120], "截图数": len(shots)})

    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "索引.csv"
    with open(index, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["序号", "姓名", "结论", "环节", "原因", "截图数"])
        w.writeheader()
        w.writerows(rows)

    print(f"批次 {run}：{len(records)} 条记录")
    print(f"  导出截图 {exported} 人 / 无截图 {missing} 人")
    print(f"  目录：{out_dir}")
    print(f"  索引：{index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
