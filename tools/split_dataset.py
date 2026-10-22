"""把运营助理数据切成 训练/验证/测试 = 70%/15%/15%。

    python tools\\split_dataset.py [--seed 42]

约束（来自知识库 case_policy）：
    · 同一候选人的所有记录必须落在同一个分组，避免泄漏
    · 分层抽样，保持「合适/不合适」比例
    · 固定随机种子，切分可复现

输出：
    data\\dataset\\train.csv / val.csv / test.csv
    data\\dataset\\split.json   （分组、种子、条数、每条记录的 id）
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "运营助理-清洗.csv"
OUT_DIR = ROOT / "data" / "dataset"

TRAIN_RATIO, VAL_RATIO, TEST_RATIO = 0.70, 0.15, 0.15


def person_key(row: dict) -> str:
    """同一候选人判重：姓名 + 学校（姓名缺失时用学校+最近公司）。"""
    name = re.sub(r"\s", "", row.get("姓名") or "")
    school = re.sub(r"\s", "", row.get("学校") or "")
    company = re.sub(r"\s", "", row.get("最近一段公司") or "")
    return f"{name}|{school}" if name else f"无名|{school}|{company}"


def stratify(rows: list[dict], seed: int) -> dict[str, list[dict]]:
    rng = random.Random(seed)

    # 先按人去重，保证同一人不跨组
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(person_key(row), []).append(row)

    # 每个"人"用第一条的标签作为分层依据
    by_label: dict[str, list[tuple[str, list[dict]]]] = {}
    for key, items in groups.items():
        label = (items[0].get("人事结论") or "").strip() or "未标"
        by_label.setdefault(label, []).append((key, items))

    splits = {"train": [], "val": [], "test": []}
    for label, entries in by_label.items():
        rng.shuffle(entries)
        n = len(entries)
        n_train = max(1, round(n * TRAIN_RATIO)) if n >= 3 else n
        n_val = max(1, round(n * VAL_RATIO)) if n - n_train >= 2 else (1 if n - n_train >= 1 else 0)
        n_test = n - n_train - n_val
        if n_test < 0:
            n_test = 0
            n_val = n - n_train
        for i, (_key, items) in enumerate(entries):
            if i < n_train:
                splits["train"].extend(items)
            elif i < n_train + n_val:
                splits["val"].extend(items)
            else:
                splits["test"].extend(items)

    for name in splits:
        rng.shuffle(splits[name])
    return splits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--file", default=str(SRC))
    args = ap.parse_args(argv)

    rows = list(csv.DictReader(open(args.file, encoding="utf-8-sig")))
    print(f"读取 {len(rows)} 条运营助理记录")
    splits = stratify(rows, args.seed)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0].keys())
    meta = {"seed": args.seed, "source": Path(args.file).name,
            "ratios": {"train": TRAIN_RATIO, "val": VAL_RATIO, "test": TEST_RATIO},
            "splits": {}}

    for name, items in splits.items():
        path = OUT_DIR / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols)
            writer.writeheader()
            writer.writerows(items)
        labels = {}
        for it in items:
            lab = it.get("人事结论") or "未标"
            labels[lab] = labels.get(lab, 0) + 1
        meta["splits"][name] = {
            "count": len(items),
            "labels": labels,
            "persons": sorted({person_key(it) for it in items}),
        }
        print(f"  {name:<5} {len(items):>3} 条  {labels}")

    (OUT_DIR / "split.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    total = sum(s["count"] for s in meta["splits"].values())
    print(f"\n合计 {total} 条；切分文件 {OUT_DIR}\\split.json")
    # 泄漏检查
    p_train = set(meta["splits"]["train"]["persons"])
    for other in ("val", "test"):
        overlap = p_train & set(meta["splits"][other]["persons"])
        print(f"  训练集 vs {other} 同一候选人重叠：{len(overlap)}"
              + (f" ⚠ {sorted(overlap)[:3]}" if overlap else " ✓"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
