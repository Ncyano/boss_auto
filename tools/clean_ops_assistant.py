"""清洗「运营助理」岗位的数据，供后续评测/训练使用。

    python tools\\clean_ops_assistant.py

输入：data\\hr-resumes-采集格式.csv（人事表格经 VLM 转换后的采集格式）
输出：data\\运营助理-清洗.csv        清洗后的分析用表
      data\\运营助理-清洗报告.txt    质量与分布统计
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

SRC = ROOT / "data" / "hr-resumes-采集格式.csv"
OUT = ROOT / "data" / "运营助理-清洗.csv"
REPORT = ROOT / "data" / "运营助理-清洗报告.txt"

TODAY = dt.date.today()
MAX_SLOTS = 10
PERIOD_RE = re.compile(r"(\d{4})[.\-/年]?(\d{1,2})?")


def parse_period(period: str) -> tuple[dt.date | None, dt.date | None, bool]:
    """返回 (开始, 结束, 是否至今)。解析不出返回 None。"""
    text = (period or "").replace(" ", "")
    if not text:
        return None, None, False
    parts = PERIOD_RE.findall(text)
    if not parts:
        return None, None, False
    current = ("至今" in text) or ("现在" in text)
    dates: list[dt.date] = []
    for year, month in parts:
        if not year:
            continue
        m = int(month) if month else 1
        m = m if 1 <= m <= 12 else 1
        try:
            dates.append(dt.date(int(year), m, 1))
        except ValueError:
            pass
    if not dates:
        return None, None, current
    start = dates[0]
    end = dates[-1] if len(dates) >= 2 else None
    return start, end, current


def months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def job_months(period: str) -> int:
    start, end, current = parse_period(period)
    if not start:
        return 0
    stop = TODAY if current else (end or start)
    if stop < start:
        stop = start
    return max(0, months_between(start, stop))


def clean_mode(text: str) -> str:
    t = (text or "").strip()
    if "非全日制" in t:
        return "非全日制"
    if "全日制" in t:
        return "全日制"
    return "未知"


def clean_name(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    if re.fullmatch(r"[\u4e00-\u9fa5]{2,6}", t) or re.fullmatch(r"[A-Za-z .'-]{2,20}", t):
        return t
    return ""


def load_schools() -> set[str]:
    path = ROOT / "data" / "schools-normalized.csv"
    if not path.exists():
        return set()
    import csv as _csv
    out = set()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in _csv.DictReader(fh):
            out.add(re.sub(r"\s", "", row.get("学校名称", "")))
    return out


def main() -> int:
    with SRC.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    ops = [r for r in rows if "运营助理" in (r.get("岗位") or "")]
    print(f"总记录 {len(rows)} 条，其中运营助理 {len(ops)} 条")

    schools = load_schools()
    cleaned: list[dict] = []
    seen: dict[tuple, str] = {}
    dupes = 0
    issues = Counter()

    for r in ops:
        name = clean_name(r.get("姓名"))
        if not name:
            issues["姓名缺失或不可用"] += 1

        works = []
        total_months = 0
        for i in range(1, MAX_SLOTS + 1):
            company = (r.get(f"第{i}家公司") or "").strip()
            title = (r.get(f"第{i}家职位") or "").strip()
            period = (r.get(f"第{i}家起止") or "").strip()
            if not (company or period):
                continue
            months = job_months(period)
            total_months += months
            works.append({"company": company, "title": title, "period": period,
                          "months": months})

        # 按结束时间倒序排列（列表顺序不可靠：有人把最早的一段写在最前面）
        def _sort_key(w):
            start, end, current = parse_period(w["period"])
            if current:
                return (1, TODAY.toordinal())
            return (0, end.toordinal() if end else 0)

        works.sort(key=_sort_key, reverse=True)

        # 最近一段结束到今天的空档
        gap = None
        if works:
            last = works[0]
            _, end, current = parse_period(last["period"])
            if current:
                gap = 0
            elif end:
                gap = max(0, months_between(end, TODAY))

        school = (r.get("学校") or "").strip()
        degree = (r.get("学历") or "").strip()
        major = (r.get("专业") or "").strip()

        # 字段串位：学校列里写的是学历或证书（大专/中专/中技/CET-4 等）
        if school and re.search(r"^(大专|专科|本科|硕士|博士|研究生|中专|中技|高中|初中)",
                                school) or re.search(r"CET|四级|六级|证书|资格", school or ""):
            if not degree and re.search(r"(大专|专科|本科|硕士|博士|研究生|中专|中技|高中)", school):
                degree = re.search(r"(大专|专科|本科|硕士|博士|研究生|中专|中技|高中)", school).group(1)
            issues["学校列串位（学历/证书误入）"] += 1
            school = ""
        mode = clean_mode(r.get("学历性质"))
        status = (r.get("求职状态") or "").strip()
        grad = (r.get("毕业年份") or "").strip()
        age_text = (r.get("年龄") or "").strip()

        try:
            age = int(age_text) if age_text.isdigit() else None
        except ValueError:
            age = None
        if age is None:
            issues["年龄缺失"] += 1
        if mode == "未知":
            issues["学历性质未知"] += 1
        if not works:
            issues["无工作经历"] += 1
        if gap is not None and gap > 6:
            issues["空档超过 6 个月"] += 1
        if not school:
            issues["学校缺失"] += 1
        elif schools and re.sub(r"\s", "", school) not in schools:
            issues["学校不在教育部名单"] += 1

        is_fresh = bool(re.search(r"应届|在校|实习生", status)) or (
            grad.isdigit() and int(grad) >= TODAY.year)

        key = (name or "", school, works[0]["company"] if works else "")
        if name and key in seen:
            dupes += 1
            issues["重复记录"] += 1
            continue
        if name:
            seen[key] = name

        cleaned.append({
            "序号": len(cleaned) + 1,
            "姓名": name,
            "年龄": age if age is not None else "",
            "学历性质": mode,
            "学历": degree,
            "学校": school,
            "专业": major,
            "毕业年份": grad,
            "应届或在校": "是" if is_fresh else "否",
            "求职状态": status,
            "工作经历条数": len(works),
            "累计工作月数": total_months,
            "最近空档月数": gap if gap is not None else "",
            "最近一段公司": works[0]["company"] if works else "",
            "最近一段职位": works[0]["title"] if works else "",
            "最近一段起止": works[0]["period"] if works else "",
            "全部工作经历": " | ".join(
                f'{w["period"]} {w["company"]}/{w["title"]}' for w in works),
            "全部职责": (r.get("第1家工作内容") or "")[:200],
            "招聘人": r.get("招聘人", ""),
            "人事结论": r.get("是否合适", ""),
            "人事原因": (r.get("人事原因") or "").strip(),
            "需复核": r.get("需复核", ""),
            "复核原因": r.get("复核原因", ""),
        })

    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(cleaned[0]))
        writer.writeheader()
        writer.writerows(cleaned)

    verdict = Counter(x["人事结论"] for x in cleaned)
    by_person = Counter((x["招聘人"], x["人事结论"]) for x in cleaned)

    lines = [
        "运营助理 数据清洗报告",
        "=" * 46,
        f"源文件：{SRC.name}（总 {len(rows)} 条）",
        f"运营助理：{len(ops)} 条；去重后 {len(cleaned)} 条（重复 {dupes} 条已剔除）",
        "",
        "人事结论分布：",
    ]
    for k, v in verdict.most_common():
        lines.append(f"  {k or '(未标)'}: {v}")
    lines.append("")
    lines.append("按招聘人：")
    for (person, result), n in sorted(by_person.items()):
        lines.append(f"  {person}  {result or '(未标)'}: {n}")
    lines += ["", "数据质量问题（出现次数）："]
    for k, v in issues.most_common():
        lines.append(f"  {k}: {v}")

    numeric = [x for x in cleaned if x["累计工作月数"]]
    if numeric:
        months = sorted(x["累计工作月数"] for x in numeric)
        mid = months[len(months) // 2]
        lines += ["", "工作年限分布：",
                  f"  中位数 {mid} 个月（约 {mid/12:.1f} 年）",
                  f"  最长 {months[-1]} 个月 / 最短 {months[0]} 个月"]

    ages = [x["年龄"] for x in cleaned if x["年龄"]]
    if ages:
        ages_sorted = sorted(ages)
        lines.append(f"  年龄：中位数 {ages_sorted[len(ages)//2]}，"
                     f"范围 {ages_sorted[0]}~{ages_sorted[-1]}")

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n清洗结果 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
