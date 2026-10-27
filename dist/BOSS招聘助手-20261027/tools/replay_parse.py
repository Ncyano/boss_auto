"""离线回放：用保存下来的 ocr_lines 重新跑一遍解析，验证修复效果。

不需要浏览器、不需要 OCR —— 直接读 -results.json 里的原始行。

    python tools/replay_parse.py                     # 最新一批
    python tools/replay_parse.py --file runs/xxx-results.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import schools  # noqa: E402
import stream_parse  # noqa: E402

RUNS = ROOT / "boss_vision" / "runs"


def replay(record: dict) -> dict:
    """把一条记录的 ocr_lines 按屏喂回解析器。"""
    lines = record.get("ocr_lines") or []
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
        "conflicts": parser.conflicts,
        "sections": parser.section_seen,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None)
    ap.add_argument("--limit", type=int, default=0, help="只看前 N 条")
    args = ap.parse_args(argv)

    path = Path(args.file) if args.file else sorted(RUNS.glob("*-results.json"))[-1]
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"回放 {path.name}（{len(data)} 条）\n")

    better = worse = same = 0
    valid_old = valid_new = 0
    edu_old_ok = edu_new_ok = 0
    shown = 0
    for rec in data:
        res = replay(rec)
        def _valid_work(ws):
            return sum(1 for w in ws
                       if (w.get("company") or "").strip() and (w.get("period") or "").strip())

        def _valid_edu(es):
            """教育条目算"有效"：校名非空且能在教育部名单里查到。"""
            n = 0
            for e in es:
                name = (e.get("school") or "").strip()
                if name and schools.lookup(name)["listed"]:
                    n += 1
            return n

        old_w, new_w = len(rec.get("work") or []), len(res["work"])
        old_e, new_e = len(rec.get("education") or []), len(res["education"])
        # 用"有效条目数"判断好坏：公司+时间齐全的工作段、有校名的教育段
        old_score = _valid_work(rec.get("work") or []) * 2 + _valid_edu(rec.get("education") or [])
        new_score = _valid_work(res["work"]) * 2 + _valid_edu(res["education"])
        mark = "=" if new_score == old_score else ("+" if new_score > old_score else "-")
        if mark == "+":
            better += 1
        elif mark == "-":
            worse += 1
        else:
            same += 1

        if args.limit and shown >= args.limit:
            continue
        shown += 1
        missing = [w for w in res["work"] if not w["company"]]
        print(f"  {mark} #{rec.get('index'):<2} {rec.get('name') or '(无名)':<8} "
              f"经历 {old_w} → {new_w}  教育 {old_e} → {new_e}  "
              f"缺公司 {len(missing)}  章节={'/'.join(res['sections'])}")
        def _valid(ws):
            return sum(1 for w in ws if (w.get("company") or "").strip() and (w.get("period") or "").strip())

        valid_old += _valid(rec.get("work") or [])
        valid_new += _valid(res["work"])
        edu_old_ok += sum(1 for e in (rec.get("education") or []) if (e.get("school") or "").strip())
        edu_new_ok += sum(1 for e in res["education"] if (e.get("school") or "").strip())

        if res["conflicts"]:
            print(f"      冲突: {res['conflicts'][0][:70]}")

    print(f"\n合计：变好 {better} 条，不变 {same} 条，变差 {worse} 条")
    print(f"有效工作经历（公司+时间齐全）：旧 {valid_old} 段 -> 新 {valid_new} 段")
    print(f"有学校名的教育条目：旧 {edu_old_ok} 段 -> 新 {edu_new_ok} 段")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
