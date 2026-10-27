"""对采集出来的 CSV 做离线筛选：学历规则 -> 工作经历门槛 ->（预留）模型评分。

用法：
    python tools\\screen_resumes.py                        # 用 runs 里最新一天的汇总表
    python tools\\screen_resumes.py --file boss_vision\\runs\\2026-10-21.csv
    python tools\\screen_resumes.py --out 自定义输出.csv
    python tools\\screen_resumes.py --show                 # 顺便逐条打印

输出列：
    序号 姓名 学校 学历 工作经历条数 教育结论 教育原因 学校匹配 经历结论 模型评分 最终结论 原因

规则文件：data\\screening-rules.json（首次运行自动生成，改完直接重跑即可）
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import screening  # noqa: E402

RUNS = ROOT / "boss_vision" / "runs"
OUT_COLUMNS = ("序号", "姓名", "学校", "学历", "工作经历条数",
               "教育结论", "教育原因", "学校匹配", "经历结论", "模型评分", "最终结论", "原因")


def latest_daily_csv() -> Path | None:
    files = sorted(p for p in RUNS.glob("*.csv") if p.stem.count("-") == 2 and len(p.stem) == 10)
    return files[-1] if files else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None, help="输入 CSV（默认 runs 里最新的日期表）")
    ap.add_argument("--out", default=None, help="输出 CSV（默认 <输入名>-screening.csv）")
    ap.add_argument("--show", action="store_true", help="逐条打印判定结果")
    ap.add_argument("--scorer", default="none", choices=("none",), help="评分器（模型接入前只有 none）")
    args = ap.parse_args(argv)

    src = Path(args.file) if args.file else latest_daily_csv()
    if not src or not src.exists():
        print("找不到输入 CSV，请用 --file 指定")
        return 2

    screening.ensure_rules_file()
    rules = screening.load_rules()

    with src.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    results = [screening.screen_row(row, rules) for row in rows]

    out = Path(args.out) if args.out else src.with_name(f"{src.stem}-screening.csv")
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(OUT_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["最终结论"]] = counts.get(r["最终结论"], 0) + 1

    print(f"输入：{src.name}（{len(rows)} 条）")
    print(f"输出：{out}")
    print(f"最低学历要求：{rules['edu']['min_level']}；"
          f"学校必须在校名单内：{'是' if rules['edu']['require_listed_school'] else '否'}；"
          f"工作经历下限：{rules['work']['min_entries']} 段")
    for verdict in ("合适", "不合适"):
        if counts.get(verdict):
            print(f"  {verdict}：{counts[verdict]} 条")
    other = {k: v for k, v in counts.items() if k not in ("合适", "不合适")}
    for k, v in other.items():
        print(f"  {k or '(空)'}：{v} 条")

    if args.show:
        print()
        for r in results:
            print(f"  {str(r['序号']):>3} {r['姓名'] or '':<8} {r['最终结论']:<4} "
                  f"{r['学校'][:16]:<18} {r['学历']:<6} 经历={r['经历结论']:<4} {r['原因']}")

    # 原因分布，便于发现系统性问题
    reasons: dict[str, int] = {}
    for r in results:
        if r["最终结论"] == "不合格":
            reasons[r["原因"]] = reasons.get(r["原因"], 0) + 1
    if reasons:
        print("\n不合格原因分布：")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>3} 条  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
