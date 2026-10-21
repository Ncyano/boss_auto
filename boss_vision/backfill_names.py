"""给历史上跑出来的 results.json 做一次性修整：

    1. 补「姓名」（默认只填空缺，--force 覆盖重算）
    2. 用新解析器重算「工作经历 / 教育经历」
    3. 用《全国普通高等学校名单》校正学校名
    4. 补「需复核 / 复核原因」

用法：
    python boss_vision\\backfill_names.py           # 只补缺失字段
    python boss_vision\\backfill_names.py --force   # 强制全部重算
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import extract  # noqa: E402
import resume_reader  # noqa: E402
import textfix  # noqa: E402

RUNS = _HERE / "runs"


def name_from_txt(path: Path) -> str:
    """用整段简历文本认姓名（上下文更完整，比按坐标更稳）。"""
    if not path.exists():
        return ""
    return resume_reader.guess_name_from_text(path.read_text(encoding="utf-8"))


def reparse_entries(rec: dict) -> bool:
    """用新解析器重算工作/教育经历；返回是否有变化。"""
    secs = rec.get("sections") or {}
    work = extract.parse_section_text(secs.get("工作经历", ""), "work")
    edu = extract.parse_section_text(secs.get("教育经历", ""), "edu")

    changed = False
    if work:
        fresh = [
            {"company": w.company, "title": w.title, "employment": w.employment,
             "period": w.period, "descriptions": w.descriptions, "tags": w.tags}
            for w in work
        ]
        if fresh != rec.get("work"):
            rec["work"] = fresh
            changed = True
    if edu:
        fresh_edu = [
            {"school": e.school, "major": e.major, "degree": e.degree, "period": e.period}
            for e in edu
        ]
        if fresh_edu != rec.get("education"):
            rec["education"] = fresh_edu
            changed = True
    return changed


def fix_schools(rec: dict) -> bool:
    """用教育部名单校正学校名（老记录无法重新解析时也能修）。"""
    edus = rec.get("education") or []
    if not edus:
        return False

    wrapped = []
    for e in edus:
        obj = type("Edu", (), {})()
        obj.school = e.get("school", "")
        obj.major = e.get("major", "")
        obj.degree = e.get("degree", "")
        obj.period = e.get("period", "")
        wrapped.append(obj)

    extract._fix_education_with_list(wrapped)

    changed = False
    for e, obj in zip(edus, wrapped):
        raw = getattr(obj, "school_raw", "")
        if raw and e.get("school_raw") != raw:
            e["school_raw"] = raw
            changed = True
        if (e.get("school", ""), e.get("major", "")) != (obj.school, obj.major):
            e["school"], e["major"] = obj.school, obj.major
            changed = True
    return changed


SECTION_MAP = {"工作经历": "work", "教育经历": "edu", "项目经历": "project"}


def fill_collect_state(rec: dict) -> bool:
    """补采集状态字段（老记录没有）：章节、完成、屏数。"""
    changed = False
    secs = rec.get("sections") or {}
    seen = [SECTION_MAP[k] for k in secs if k in SECTION_MAP]
    if rec.get("section_seen") != seen:
        rec["section_seen"] = seen
        changed = True
    if rec.get("complete") is None:
        rec["complete"] = bool(rec.get("reached_education"))
        changed = True
    if not rec.get("screen_count"):
        rec["screen_count"] = int(rec.get("steps") or 0) + 1
        changed = True
    return changed


def mark_review(rec: dict) -> bool:
    """补「需复核 / 复核原因」：教育经历没读到、或工作经历 0 条。"""
    reasons: list[str] = []
    if not rec.get("reached_education"):
        reasons.append(f"教育经历未读到（{rec.get('stopped_reason') or '未知原因'}）")
    works = rec.get("work") or []
    if not works:
        reasons.append("工作经历 0 条")
    else:
        missing_title = sum(1 for w in works
                            if (w.get("company") or "").strip() and not (w.get("title") or "").strip())
        missing_company = sum(1 for w in works if not (w.get("company") or "").strip())
        if missing_title:
            reasons.append(f"{missing_title} 段经历缺职位")
        if missing_company:
            reasons.append(f"{missing_company} 段经历缺公司名")

    want = bool(reasons)
    want_reason = "；".join(reasons)
    if rec.get("needs_review") != want or rec.get("review_reason") != want_reason:
        rec["needs_review"] = want
        rec["review_reason"] = want_reason
        return True
    return False


def main() -> int:
    force = "--force" in sys.argv

    total_files = 0
    total_records = 0
    filled = 0

    for path in sorted(RUNS.glob("*-results.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"跳过 {path.name}: {exc}")
            continue
        if not isinstance(data, list) or not data:
            continue

        stem = path.name.replace("-results.json", "")
        changed = False

        for rec in data:
            total_records += 1

            if rec.get("name") and not force:
                filled += 1
            else:
                nm = name_from_txt(RUNS / f"{stem}-resume-{rec.get('index')}.txt")
                rec["name"] = nm
                if nm:
                    filled += 1
                changed = True

            if reparse_entries(rec):
                changed = True
            if fix_schools(rec):
                changed = True
            if fill_collect_state(rec):
                changed = True
            if mark_review(rec):
                changed = True

        if changed:
            path.write_text(json.dumps(textfix.fix_obj(data), ensure_ascii=False, indent=2),
                            encoding="utf-8")
            total_files += 1
            print(f"已更新 {path.name}（{len(data)} 条）")

    print(f"\n共处理 {total_records} 条记录，其中 {filled} 条有姓名；改写 {total_files} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
