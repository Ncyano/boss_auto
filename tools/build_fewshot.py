"""从训练集构造少样本示例（DeepSeek 不支持微调，所以"训练"= 选示例 + 校准提示词）。

    python tools\\build_fewshot.py --n 6

做法：
    · 只用 **train.csv** 里的记录，绝不碰 val / test
    · 按「合适 / 不合适」均衡抽取
    · 优先选职责写得具体、信息完整的（示例越具体，模型越能学到人事的口味）
    · 示例里的 assistant 回答用人事原话精简，训练模型对齐人事口径

输出：data\\fewshot.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import kb as kbmod          # noqa: E402
import screening            # noqa: E402

TRAIN = ROOT / "data" / "dataset" / "train.csv"
OUT = ROOT / "data" / "fewshot.json"


def work_text(row: dict) -> str:
    history = (row.get("全部工作经历") or "").strip()
    if not history:
        return "（无工作经历）"
    return "\n".join(f"{i}) {p.strip()}"
                     for i, p in enumerate(history.split(" | "), 1))


def informativeness(row: dict) -> int:
    """信息量打分：有经历、职责具体、文本长度适中偏高。"""
    score = 0
    if (row.get("全部工作经历") or "").strip():
        score += 30
    duty = (row.get("全部职责") or "").strip()
    score += min(len(duty) // 10, 30)
    reason = (row.get("人事原因") or "").strip()
    score += min(len(reason) // 4, 25)
    try:
        score += min(int(row.get("工作经历条数") or 0) * 5, 20)
    except ValueError:
        pass
    return score


def build_example(row: dict) -> dict:
    verdict = "建议合适" if (row.get("人事结论") or "") == "合适" else "不合适"
    reason = (row.get("人事原因") or "").strip()
    reason = re.sub(r"\s+", " ", reason)[:120]
    if not reason:
        reason = "按人事判定"
    answer = {
        "rule_results": [],
        "missing_information": [],
        "final_recommendation": verdict,
        "decision_basis": reason,
    }
    return {
        "sample_id": row.get("序号", ""),
        "label": row.get("人事结论", ""),
        "user": f"候选人工作经历：\n{work_text(row)}",
        "assistant": json.dumps(answer, ensure_ascii=False),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6, help="总示例数（按标签均衡）")
    ap.add_argument("--train", default=str(TRAIN))
    args = ap.parse_args(argv)

    rows = list(csv.DictReader(open(args.train, encoding="utf-8-sig")))
    kb = kbmod.load()
    rules = screening.load_rules()

    def passes_hard(row: dict) -> bool:
        """只保留"硬规则已通过"的样本：这些人的人事结论才真正是在判工作经历。"""
        srow = {k: row.get(k, "") for k in (
            "序号", "姓名", "学校", "学历", "专业", "学历性质", "年龄", "求职状态",
            "工作经历条数")}
        for i in range(1, 11):
            for suffix in ("公司", "职位", "起止"):
                key = f"第{i}家{suffix}"
                srow[key] = row.get(key, "")
        srow.update({"采集完成": "是", "需复核": "否", "复核原因": "", "冲突数": 0})
        return screening.evaluate_kb(srow, kb)["verdict"] == ""

    rows = [r for r in rows if passes_hard(r)]
    good = [r for r in rows if (r.get("人事结论") or "") == "合适"]
    bad = [r for r in rows if (r.get("人事结论") or "") == "不合适"]

    good.sort(key=informativeness, reverse=True)
    bad.sort(key=informativeness, reverse=True)

    if args.n <= 0:
        picked = []
    else:
        half = args.n // 2
        picked = good[:half] + bad[:args.n - half]
    examples = [build_example(r) for r in picked]

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"source": Path(args.train).name, "count": len(examples),
                               "examples": examples}, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    n_good = min(len(good), args.n // 2)
    n_bad = min(len(bad), args.n - n_good)
    print(f"从训练集 {len(rows)} 条中选出 {len(examples)} 条示例（合适 {n_good} / 不合适 {n_bad}）")
    for ex in examples:
        print(f"  [{ex['label']:<4}] {ex['user'].splitlines()[1][:56]}")
    print(f"\n-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
