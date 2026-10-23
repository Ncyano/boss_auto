"""用当前已校准的口径（规则 + few-shot + DeepSeek）判断新人事表的 104 条运营助理记录，
和人事的人工结论逐条对照，找出不一致的地方。

用法：
  python tools/eval_new_104.py --limit 5     # 先试 5 条
  python tools/eval_new_104.py               # 全量 104 条
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))
sys.path.insert(0, str(REPO / "tools"))

import judge                                   # noqa: E402
import screening                               # noqa: E402
import train_eval                              # noqa: E402

SRC = REPO / "data" / "新人事-解析.csv"
OUT = REPO / "data" / "新人事-104-判定对照.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(SRC, encoding="utf-8-sig")))
    if args.limit:
        rows = rows[:args.limit]
    rules = screening.load_rules()

    out_rows: list[dict] = []
    for i, raw in enumerate(rows, 1):
        srow = train_eval.screen_row_of(raw)
        try:
            res = screening.screen_row(srow, rules)
        except Exception as exc:
            res = {"最终结论": "异常", "处理状态": "调用失败", "原因": str(exc)[:80]}

        truth = (raw.get("人事结论") or "").strip()
        got = res.get("最终结论") or ""
        status = res.get("处理状态") or ""
        # 技术原因没判定出来的，不计入准确率
        if status in ("调用失败", "采集不完整") or got not in ("合适", "不合适"):
            mark = "跳过"
        elif got == truth:
            mark = "✓"
        else:
            mark = "✗"

        out_rows.append({
            "序号": raw.get("序号", ""),
            "招聘人": raw.get("招聘人", ""),
            "姓名": raw.get("姓名", ""),
            "人事结论": truth,
            "模型结论": got,
            "处理状态": status,
            "核对": mark,
            "模型原因": (res.get("原因") or "")[:120],
            "人事原因": (raw.get("人事原因") or "")[:120],
        })
        if not args.quiet:
            print(f"  [{i}/{len(rows)}] {mark} {raw.get('招聘人','')}-{raw.get('序号','')} "
                  f"{raw.get('姓名','')[:8]:<10} 人工={truth:<4} 模型={got:<4} {status}", flush=True)

    fields = list(out_rows[0].keys())
    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    judged = [r for r in out_rows if r["核对"] in ("✓", "✗")]
    hit = sum(1 for r in judged if r["核对"] == "✓")
    print(f"\n对照结果 -> {OUT}")
    print(f"  总条数 {len(out_rows)}；可判定 {len(judged)}；跳过 {len(out_rows) - len(judged)}")
    if judged:
        acc = hit / len(judged)
        print(f"  一致率 {hit}/{len(judged)} = {acc:.3f}")
        tp = sum(1 for r in judged if r["核对"] == "✓" and r["人事结论"] == "合适")
        fp = sum(1 for r in judged if r["核对"] == "✗" and r["人事结论"] == "不合适")
        fn = sum(1 for r in judged if r["核对"] == "✗" and r["人事结论"] == "合适")
        tn = sum(1 for r in judged if r["核对"] == "✓" and r["人事结论"] == "不合适")
        print(f"  混淆矩阵：真合适 {tp + fn}（判对 {tp}） / 真不合适 {tn + fp}（判对 {tn}）")
        if tp + fp:
            print(f"  精确率 {tp / (tp + fp):.3f}  召回率 {tp / (tp + fn):.3f}")
    print()
    print("  跳过的技术原因：", dict(Counter(r["处理状态"] for r in out_rows if r["核对"] == "跳过")))
    print("  不一致明细：")
    for r in out_rows:
        if r["核对"] == "✗":
            print(f"    {r['姓名'][:8]:<10} 人工={r['人事结论']:<4} 模型={r['模型结论']:<4} "
                  f"| 模型：{r['模型原因'][:52]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
