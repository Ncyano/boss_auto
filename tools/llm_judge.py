"""按知识库跑证据匹配评测：硬规则（kb.py）→ 工作证据（DeepSeek）→ 与人事结论比对。

    python tools\\llm_judge.py --limit 5        # 先试 5 条
    python tools\\llm_judge.py                 # 跑运营助理全部 51 条

输入：data\\运营助理-清洗.csv
输出：data\\llm-judge-结果.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import judge  # noqa: E402
import kb as kbmod  # noqa: E402
import screening  # noqa: E402

DATA = ROOT / "data"


def build_work_text(row: dict) -> str:
    history = (row.get("全部工作经历") or "").strip()
    if not history:
        return "（简历里没有工作经历）"
    lines = [f"{i}) {p.strip()}" for i, p in enumerate(history.split(" | "), 1)]
    duty = (row.get("全部职责") or "").strip()
    if duty:
        lines.append(f"补充职责原文：{duty}")
    return "\n".join(lines)


def as_screening_row(row: dict) -> dict:
    return {
        "序号": row.get("序号", ""), "姓名": row.get("姓名", ""),
        "学校": row.get("学校", ""), "学历": row.get("学历", ""),
        "专业": row.get("专业", ""), "学历性质": row.get("学历性质", ""),
        "年龄": row.get("年龄", ""), "求职状态": row.get("求职状态", ""),
        "工作经历条数": row.get("工作经历条数", ""),
        "采集完成": "是", "需复核": "否", "复核原因": "", "冲突数": 0,
        "第1家公司": row.get("最近一段公司", ""), "第1家起止": row.get("最近一段起止", ""),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DATA / "运营助理-清洗.csv"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(DATA / "llm-judge-结果.csv"))
    ap.add_argument("--skip-kb-reject", action="store_true",
                    help="跳过硬规则已判死的（只评工作证据层）")
    args = ap.parse_args(argv)

    kb = kbmod.load()
    cfg = judge.load_config()
    if not judge.available():
        print("没有 DEEPSEEK_API_KEY，先设置：\n"
              '  setx DEEPSEEK_API_KEY "sk-..."\n'
              "重开终端后再跑")
        return 2

    rows = list(csv.DictReader(open(args.file, encoding="utf-8-sig")))

    picked = []
    for r in rows:
        srow = as_screening_row(r)
        kb_out = screening.evaluate_kb(srow, kb)
        if args.skip_kb_reject and kb_out["verdict"] == "不合适":
            continue
        picked.append((r, kb_out))
    if args.limit:
        picked = picked[: args.limit]

    print(f"知识库 v{kb.get('version')}，模型 {cfg['model']}，待判 {len(picked)} 条\n")

    out_rows = []
    stat = {"合适": 0, "不合适": 0, "待复核": 0, "失败": 0}
    tp = fp = tn = fn = 0
    tokens = 0

    for i, (row, kb_out) in enumerate(picked, 1):
        name = row.get("姓名") or "(无名)"
        actual = (row.get("人事结论") or "").strip()

        # 硬规则已判死：不再调用模型
        if kb_out["verdict"] == "不合适":
            final, reason, evidence = "不合适", f"硬规则：{kb_out['reason']}", ""
            stat["不合适"] += 1
        else:
            try:
                res = judge.judge(build_work_text(row), kb, cfg)
            except Exception as exc:
                stat["失败"] += 1
                print(f"  [{i}/{len(picked)}] ✗ {name} 调用失败：{str(exc)[:70]}")
                out_rows.append({**{k: row.get(k, "") for k in ("序号", "姓名", "人事结论", "人事原因")},
                                 "机判结论": "调用失败", "机判理由": str(exc)[:120],
                                 "证据": "", "硬规则提示": kb_out["reason"]})
                continue
            tokens += int((res.get("usage") or {}).get("total_tokens") or 0)
            rec = res["recommendation"]
            final = {"建议合适": "合适", "不合适": "不合适", "待复核": "待复核"}[rec]
            reason = res["decision_basis"]
            evidence = " / ".join(
                f"{r['rule_id']}:{r['status']}「{r['evidence'][0][:40]}」"
                for r in res["rule_results"] if r["evidence"])
            stat[final] += 1

        if final == "不合适" and actual == "不合适":
            tp += 1
        elif final == "不合适" and actual == "合适":
            fp += 1
        elif final == "合适" and actual == "合适":
            tn += 1
        elif final == "合适" and actual == "不合适":
            fn += 1

        mark = "✓" if final == actual else ("?" if final == "待复核" else "✗")
        print(f"  [{i}/{len(picked)}] {mark} {name:<8} 人事={actual or '—':<4} 机判={final:<4} {reason[:36]}")

        out_rows.append({
            **{k: row.get(k, "") for k in ("序号", "姓名", "人事结论", "人事原因")},
            "机判结论": final,
            "机判理由": reason,
            "证据": evidence[:300],
            "硬规则提示": kb_out["reason"] or "；".join(kb_out["advisories"])[:200],
            "知识库版本": kb.get("version", ""),
        })

    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out_rows[0]))
        writer.writeheader()
        writer.writerows(out_rows)

    judged = tp + fp + tn + fn
    print("\n=== 指标 ===")
    print(f"  已判 {judged} 条（待复核 {stat['待复核']} / 失败 {stat['失败']}）")
    if tp + fp:
        print(f"  判不合适的精确率：{tp/(tp+fp):.0%}（{tp}/{tp+fp}）")
    if tp + fn:
        print(f"  抓不合格的召回率：{tp/(tp+fn):.0%}（{tp}/{tp+fn}）")
    if judged:
        print(f"  与人事一致：{(tp+tn)/judged:.0%}（{tp+tn}/{judged}）")
    print(f"  token 合计：{tokens}")
    print(f"\n结果 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
