"""用保存下来的原始证据（ocr_lines）重算结构化结果。

为什么需要它：
    `-results.json` 里的 work / education 是采集当时用**当时的解析器**跑出来的。
    解析器修好后，不必重新打开浏览器，只要把 ocr_lines 重新喂一遍就能得到新结果。

用法：
    python tools\\repair_from_evidence.py                 # 处理所有批次
    python tools\\repair_from_evidence.py --file runs\\xxx-results.json
    python tools\\repair_from_evidence.py --dry-run       # 只报告不写回
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import stream_parse  # noqa: E402

RUNS = ROOT / "boss_vision" / "runs"


def replay_record(rec: dict) -> dict | None:
    """把一条记录的 ocr_lines 按屏重新解析，返回新的 work/education。"""
    lines = rec.get("ocr_lines") or []
    if not lines:
        return None

    by_screen: dict[int, list[dict]] = {}
    for ln in lines:
        by_screen.setdefault(int(ln.get("step", 0)), []).append({
            "text": ln.get("t", ""),
            "score": ln.get("s", 0),
            "box": ln.get("b", [0, 0, 0, 0]),
        })

    parser = stream_parse.StreamParser()
    for screen in sorted(by_screen):
        parser.feed(by_screen[screen], screen=screen)
    parser.close()

    return {
        "work": parser.work,
        "education": parser.education,
        "projects": parser.projects,
        "campus": parser.campus,
        "conflicts": parser.conflicts,
        "section_seen": parser.section_seen,
    }


def review_reasons(rec: dict, work: list, education: list, conflicts: list) -> list[str]:
    """按业务规则重算复核原因（与采集时的逻辑保持一致）。"""
    reasons: list[str] = []
    if not rec.get("reached_education"):
        reasons.append(f"教育经历未读到（{rec.get('stopped_reason') or '未知原因'}）")
    if not rec.get("complete"):
        reasons.append("采集未完成")

    if not work:
        reasons.append("工作经历 0 条")
    else:
        miss_title = sum(1 for w in work
                         if (w.get("company") or "").strip() and not (w.get("title") or "").strip())
        miss_company = sum(1 for w in work if not (w.get("company") or "").strip())
        if miss_title:
            reasons.append(f"{miss_title} 段经历缺职位")
        if miss_company:
            reasons.append(f"{miss_company} 段经历缺公司名")

    if not education:
        reasons.append("第一段教育缺学校")
    elif not (education[0].get("school") or "").strip():
        reasons.append("第一段教育缺学校")
    elif not (education[0].get("degree") or "").strip():
        reasons.append("第一段教育缺学历")

    if conflicts:
        reasons.append(f"{len(conflicts)} 处跨屏冲突")
    return reasons


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    files = [Path(args.file)] if args.file else sorted(RUNS.glob("*-results.json"))
    if not files:
        print("runs/ 里没有 results.json")
        return 2

    total = changed = improved = 0
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            continue
        file_changed = False

        for rec in data:
            total += 1
            res = replay_record(rec)
            if res is None:
                continue

            old_w = sum(1 for w in (rec.get("work") or [])
                        if (w.get("company") or "").strip() and (w.get("period") or "").strip())
            new_w = sum(1 for w in res["work"]
                        if (w.get("company") or "").strip() and (w.get("period") or "").strip())

            if res["work"] != (rec.get("work") or []):
                rec["work"] = res["work"]
                file_changed = True
            if res["education"] != (rec.get("education") or []):
                rec["education"] = res["education"]
                file_changed = True
            if res["projects"]:
                rec["projects"] = res["projects"]
            if res["campus"]:
                rec["campus"] = res["campus"]
            rec["conflicts"] = res["conflicts"]
            rec["section_seen"] = res["section_seen"]

            reasons = review_reasons(rec, res["work"], res["education"], res["conflicts"])
            want = bool(reasons)
            rec["needs_review"] = want
            rec["review_reason"] = "；".join(reasons)

            if new_w > old_w:
                improved += 1

        if file_changed:
            changed += 1
            if not args.dry_run:
                text = json.dumps(data, ensure_ascii=False, indent=2)
                path.write_text(text, encoding="utf-8")

        print(f"{path.name}: {len(data)} 条{'（已更新）' if file_changed and not args.dry_run else ''}")

    print(f"\n共 {total} 条：解析结果变好 {improved} 条，"
          f"{'需要写入 ' + str(changed) + ' 个文件' if not args.dry_run else 'dry-run 未写入'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
