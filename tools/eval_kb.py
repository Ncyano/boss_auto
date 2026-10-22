"""用知识库评估清洗后的数据，并与人事结论比对。

    python tools\\eval_kb.py                       # 运营助理 51 条
    python tools\\eval_kb.py --file data\\运营助理-清洗.csv

输出：判定分布、硬规则精确率、误杀清单、需要工作证据判断的清单
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import kb as kbmod  # noqa: E402
import screening  # noqa: E402

_RANGE = r"\d{4}(?:[.\-/年]\d{1,2})?[月]?"
_SEP = r"\s*[-–~至]\s*"
PERIOD_HEAD = re.compile(rf"^({_RANGE}(?:{_SEP}{_RANGE})?(?:{_SEP}(?:至今|现在))?)")


def split_history(text: str) -> dict[str, str]:
    """把「起止 公司/职位 | 起止 公司/职位」拆成第 N 家槽位。"""
    slots: dict[str, str] = {}
    parts = [p.strip() for p in (text or "").split("|") if p.strip()]
    for i, part in enumerate(parts, 1):
        m = PERIOD_HEAD.match(part)
        if m:
            period = m.group(1).strip()
            rest = part[m.end():].strip()
        else:
            period, rest = "", part
        company, _, title = rest.partition("/")
        slots[f"第{i}家公司"] = company.strip()
        slots[f"第{i}家职位"] = title.strip()
        slots[f"第{i}家起止"] = period
    return slots


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "data" / "运营助理-清洗.csv"))
    ap.add_argument("--show-review", action="store_true", default=True)
    args = ap.parse_args(argv)

    rows = list(csv.DictReader(open(args.file, encoding="utf-8-sig")))
    rules = screening.load_rules()
    kbdata = kbmod.load()

    stats: dict[str, int] = {}
    tp = fp = 0
    wrong: list[tuple[str, str]] = []
    review: list[tuple[str, str, str]] = []

    for r in rows:
        row = {
            "序号": r.get("序号", ""), "姓名": r.get("姓名", ""),
            "学校": r.get("学校", ""), "学历": r.get("学历", ""),
            "专业": r.get("专业", ""), "学历性质": r.get("学历性质", ""),
            "年龄": r.get("年龄", ""), "求职状态": r.get("求职状态", ""),
            "工作经历条数": r.get("工作经历条数", ""),
            "采集完成": "是", "需复核": "否", "复核原因": "", "冲突数": 0,
        }
        row.update(split_history(r.get("全部工作经历", "")))
        out = screening.screen_row(row, rules)
        verdict = out["最终结论"]
        stats[verdict] = stats.get(verdict, 0) + 1

        actual = r.get("人事结论", "")
        if verdict == "不合格":
            if actual == "不合适":
                tp += 1
            else:
                fp += 1
                wrong.append((r.get("姓名") or "(无名)", out["原因"]))
        elif verdict != "合格":
            review.append((r.get("姓名") or "(无名)", actual, out["原因"][:50]))

    print(f"知识库 v{kbdata.get('version')}  共 {len(rows)} 条")
    print("判定分布:", stats)
    total = tp + fp
    if total:
        print(f"硬规则判死 {total} 条，与人事一致 {tp} 条 -> 精确率 {tp/total:.0%}")
    if wrong:
        print("\n误杀（机判不合格但人事合适）：")
        for name, why in wrong:
            print(f"   {name:<8} {why[:60]}")
    if args.show_review and review:
        print(f"\n进入工作证据判断（{len(review)} 条）：")
        for name, actual, why in review:
            print(f"   {name:<8} 人事={actual or '—':<4} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
