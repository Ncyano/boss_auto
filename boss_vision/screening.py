"""离线简历筛选：学历规则 + 工作经历门槛 +（预留）工作经历二分类评分。

判定顺序与业务约定：
    1. 只取**第一段教育经历**（默认它就是最高学历）。
    2. 学校必须在《全国普通高等学校名单》内（成人高校单独标记）；学历层次不低于配置门槛。
    3. 工作经历条数为 0 -> 直接不合格（不进入模型）。
    4. 通过前两步后，交给工作经历评分器；未接入模型时输出「待评分」。

    python tools\\screen_resumes.py                          # 用最新一天的汇总表
    python tools\\screen_resumes.py --file runs\\2026-10-21.csv
    python tools\\screen_resumes.py --threshold 0.6          # 将来接模型时给阈值
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RULES_JSON = DATA / "screening-rules.json"

# 学历层次高低（数字越大越高）
LEVELS = {
    "博士": 8, "硕士": 7, "研究生": 7, "MBA": 7, "EMBA": 7,
    "本科": 6, "学士": 6,
    "大专": 5, "专科": 5, "高职": 5, "大学专科": 5,
    "中专": 4, "中技": 4, "职高": 4, "技校": 4,
    "高中": 3, "初中": 2, "小学": 1,
}

DEFAULT_RULES = {
    "edu": {
        "min_level": "大专",          # 低于这个层次判不合格
        "require_listed_school": True,  # 学校必须在教育部名单内
        "adult_school": "flag",        # flag=标出但不拦；reject=判不合格；ignore=不管
    },
    "work": {
        "min_entries": 1,             # 工作经历条数下限，0 条判不合格
    },
    "model": {
        "threshold_pass": None,       # 高于=合适（未接模型时留空）
        "threshold_reject": None,     # 低于=不合适；中间=待复核
    },
}

# 结论常量
PASS = "合适"
REJECT = "不合适"
REVIEW = "不合适"      # 二元口径：不再有中间态
COLLECT_REVIEW = "不合适"      # 二元口径：采集问题也按不合适收口


def load_rules(path: Path | None = None) -> dict:
    path = path or RULES_JSON
    rules = json.loads(json.dumps(DEFAULT_RULES))     # 深拷贝默认值
    if path.exists():
        try:
            custom = json.loads(path.read_text(encoding="utf-8"))
            for section, values in custom.items():
                if section.startswith("_"):
                    continue
                if isinstance(values, dict) and isinstance(rules.get(section), dict):
                    rules[section].update(values)
                else:
                    rules[section] = values
        except Exception as exc:
            print(f"[warn] 规则文件读取失败，使用默认值：{exc}")
    return rules


def degree_level(text: str) -> int | None:
    """把「本科」「大专」「中专/中技」这类文本折成层次数字。"""
    t = (text or "").strip()
    if not t:
        return None
    best: int | None = None
    for word, level in LEVELS.items():
        if word in t:
            best = level if best is None else max(best, level)
    return best


# 「需复核」里属于"业务事实"的原因（不是采集问题），其余都按采集质量问题处理
BUSINESS_REASONS = ("工作经历 0 条",)


def _has_section(sections: str, code: str, label: str) -> bool:
    """「读到章节」列可能是英文代码（work/edu）也可能是中文小节名。"""
    return code in sections or label in sections


def screen_collection(row: dict, rules: dict) -> dict:
    """采集质量闸门：数据不可信时不允许进入学历判断和模型判断。

    区分两种"没有工作经历"：
      · 工作经历章节读到了，但确实 0 条   -> 业务事实，可判不合格
      · 工作经历章节没读到 / 读取未完成   -> 采集问题，进"采集待复核"
    """
    problems: list[str] = []

    opened = str(row.get("已打开", "")).strip()
    if opened and opened not in ("是", "True", "true", "1"):
        problems.append("简历未成功打开")

    sections = str(row.get("读到章节", "") or "")
    completed = str(row.get("采集完成", "")).strip()

    review_flag = str(row.get("需复核", "")).strip() == "是"
    if review_flag:
        raw_reasons = [r.strip() for r in str(row.get("复核原因", "")).split("；") if r.strip()]
        for reason in raw_reasons:
            if reason in BUSINESS_REASONS and _has_section(sections, "work", "工作经历"):
                continue            # 章节读到了 -> 确实没写经历，交给业务规则
            problems.append(reason)

    if completed == "否":
        problems.append("采集未完成")

    # 补采后仍缺失的字段（姓名/学校/学历/职责/公司名…）
    missing_raw = str(row.get("缺失字段", "") or "").strip()
    if missing_raw:
        problems.append(f"采集缺失：{missing_raw[:60]}")

    # 跨屏冲突（含姓名分歧）一律不进后续流程
    try:
        conflicts = int(row.get("冲突数") or 0)
    except (TypeError, ValueError):
        conflicts = 0
    if conflicts > 0:
        problems.append(f"{conflicts} 处冲突待核对")

    try:
        work_count = int(row.get("工作经历条数") or 0)
    except (TypeError, ValueError):
        work_count = 0
    if work_count == 0 and sections and not _has_section(sections, "work", "工作经历"):
        problems.append("未读到工作经历章节")

    # 第一段教育的必要字段（只取第一段，所以这组字段必须齐）
    school = (row.get("学校") or "").strip()
    degree = (row.get("学历") or "").strip()
    if not school:
        problems.append("第一段教育缺学校")
    if not degree:
        problems.append("第一段教育缺学历")

    if problems:
        return {"collect_ok": False, "collect_reason": "；".join(dict.fromkeys(problems))}
    return {"collect_ok": True, "collect_reason": ""}


def screen_education(row: dict, rules: dict) -> dict:
    """只看第一段教育（CSV 里就是「学校/专业/学历/教育起止」那组列）。"""
    import schools

    edu_rules = rules["edu"]
    school = (row.get("学校") or "").strip()
    degree_text = (row.get("学历") or "").strip()

    if not school and not degree_text:
        return {"edu_result": REVIEW, "edu_reason": "没有解析到教育经历"}
    if not school:
        return {"edu_result": REVIEW, "edu_reason": "缺学校信息，无法核对名单"}

    res = schools.lookup(school) if school else {"listed": False, "matched": "", "method": "none",
                                                 "candidates": []}
    school_list = res.get("list", "")
    notes = []

    if edu_rules.get("require_listed_school"):
        if not res["listed"]:
            return {
                "edu_result": REJECT,
                "edu_reason": "学校不在指定名单范围" + (f"（疑似：{'/'.join(res['candidates'])}）"
                                                    if res.get("candidates") else ""),
                "school_list": school_list,
                "school_matched": res.get("matched", ""),
            }
        if res["method"] == "alias":
            notes.append(f"曾用名→{res['matched']}")

    if school_list == "adult":
        mode = edu_rules.get("adult_school", "flag")
        if mode == "reject":
            return {"edu_result": REJECT, "edu_reason": "成人高校不在指定范围",
                    "school_list": school_list, "school_matched": res.get("matched", "")}
        notes.append("成人高校")

    need = degree_level(edu_rules.get("min_level", "")) or 0
    got = degree_level(degree_text)
    if got is None:
        return {"edu_result": REVIEW, "edu_reason": "学历字段缺识别", "school_list": school_list,
                "school_matched": res.get("matched", ""), "note": "；".join(notes)}
    if got < need:
        return {"edu_result": REJECT, "edu_reason": f"学历低于{edu_rules.get('min_level')}",
                "school_list": school_list, "school_matched": res.get("matched", ""),
                "note": "；".join(notes)}

    return {"edu_result": PASS, "edu_reason": "；".join(notes) or "符合",
            "school_list": school_list, "school_matched": res.get("matched", "")}


def work_text(row: dict) -> str:
    """把各段工作经历拼成模型输入文本（职位 + 时间 + 职责，公司名不进特征）。

    不再限制段数：CSV 里有多少个「第 N 家…」列就用多少（导出时不再截断到 10 段）。
    """
    import re as _re

    slots = sorted({int(m.group(1)) for key in row
                    if (m := _re.fullmatch(r"第(\d+)家公司", key or ""))})
    parts: list[str] = []
    for i in slots:
        title = (row.get(f"第{i}家职位") or "").strip()
        period = (row.get(f"第{i}家起止") or "").strip()
        content = (row.get(f"第{i}家工作内容") or "").strip()
        company = (row.get(f"第{i}家公司") or "").strip()
        if not (title or content or company):
            continue
        head = " ".join(x for x in (title, period) if x)
        parts.append(f"{head}：{content}" if content else head)
    return "\n".join(parts)


def screen_work(row: dict, rules: dict, scorer=None, threshold_pass=None, threshold_reject=None) -> dict:
    work_rules = rules["work"]
    try:
        count = int(row.get("工作经历条数") or 0)
    except (TypeError, ValueError):
        count = 0

    if count < int(work_rules.get("min_entries", 1)):
        return {"work_result": REJECT, "work_reason": "无工作经历", "score": None}

    text = work_text(row)
    if not text.strip():
        return {"work_result": REJECT, "work_reason": "工作经历无可用文本", "score": None}

    if scorer is None:
        return {"work_result": "待评分", "work_reason": "未接入模型", "score": None,
                "text_len": len(text)}

    score = float(scorer(text))
    if threshold_pass is not None and score >= float(threshold_pass):
        verdict = PASS
    elif threshold_reject is not None and score <= float(threshold_reject):
        verdict = REJECT
    else:
        verdict = REVIEW
    return {"work_result": verdict, "work_reason": f"评分 {score:.3f}", "score": round(score, 3),
            "text_len": len(text)}


# ---------------- 人事规则（来自 data\\hr-rules.json） ----------------

HR_RULES_DEFAULT = {
    "年龄": {"上限": 30, "建议范围": [22, 28]},
    "学历": {"要求全日制": True, "最低": "大专"},
    "经验": {"最低年数": 0.5, "排除": ["应届生", "实习生"]},
    "空档期": {"最长月数": 6},
    "同行": {"黑名单": ["一硕", "云上源"]},
}


def load_hr_rules() -> dict:
    """读人事规则；没有配置文件就用内置默认值。"""
    path = ROOT / "data" / "hr-rules.json"
    rules = json.loads(json.dumps(HR_RULES_DEFAULT))
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            # hr-rules.json 是给人看的结构，做一次映射
            if isinstance(raw.get("年龄"), dict):
                up = str(raw["年龄"].get("运营助理上限") or "").strip()
                if up.isdigit():
                    rules["年龄"]["上限"] = int(up)
            if "同行" in raw and isinstance(raw["同行"], dict):
                black = raw["同行"].get("排除") or []
                if black:
                    rules["同行"]["黑名单"] = [str(x) for x in black]
            if isinstance(raw.get("规则阈值"), dict):
                for key, value in raw["规则阈值"].items():
                    if isinstance(rules.get(key), dict):
                        rules[key].update(value)
        except Exception as exc:
            print(f"[warn] hr-rules.json 读取失败：{exc}")
    return rules


def _months_between(year: int, month: int, now=None) -> int:
    import datetime as _dt
    now = now or _dt.date.today()
    return (now.year - year) * 12 + (now.month - month)


def _last_job_gap_months(work: list[dict]) -> int | None:
    """最近一段工作结束到今天的月数；「至今」返回 0；解析不出返回 None。"""
    import re as _re

    latest: int | None = None
    for w in work or []:
        period = (w.get("period") or "").replace(" ", "")
        if not period:
            continue
        if "至今" in period or "现在" in period:
            return 0
        m = _re.findall(r"(\d{4})[.\-/年]?(\d{1,2})?", period)
        if len(m) >= 2:
            year, month = m[-1]
            if month:
                gap = _months_between(int(year), int(month))
                # 取所有经历里"最晚结束"的那段，列表顺序不可靠
                latest = gap if latest is None else min(latest, gap)
    return latest


def screen_hr_rules(row: dict, rules: dict, work: list[dict] | None = None) -> dict:
    """按人事表口径做的"提示项"。

    注意：这些条件在知识库里分别是 P01（经验）/P02（空档）/P03（同行）/P04（在校实习）
    和 X01（年龄），状态是"待确认"或"已排除"，**不得用于淘汰**。
    这里只返回提示文案，是否淘汰由 kb.evaluate 决定。
    """
    advisories: list[str] = []

    try:
        age = int(row.get("年龄") or 0)
    except (TypeError, ValueError):
        age = 0
    if age:
        advisories.append(f"年龄 {age}（X01：不作为筛选特征）")

    mode = (row.get("学历性质") or "").strip()
    if "非全日制" in mode:
        advisories.append("学历非全日制（E02：属于硬规则，由 kb 判定）")

    status = (row.get("求职状态") or "").strip()
    if status and any(k in status for k in ("应届", "在校", "实习")):
        advisories.append(f"求职状态「{status}」（P04 待确认，仅提示）")

    if work is None:
        work = [{"period": row.get(f"第{i}家起止")} for i in range(1, 11)
                if row.get(f"第{i}家起止")]
    gap = _last_job_gap_months(work)
    if gap is not None and gap > 6:
        advisories.append(f"最近空档 {gap} 个月（P02 待确认，仅提示）")

    text = " ".join((row.get(f"第{i}家公司") or "") for i in range(1, 11))
    for kw in ("一硕", "一数", "云上源", "谷一科技"):
        if kw in text:
            advisories.append(f"疑似同行 {kw}（P03 待确认，需核实）")

    return {"hr_ok": True, "hr_reason": "", "advisories": advisories}


def _total_work_months(work: list[dict]) -> int | None:
    """累计工作月数（重叠部分按最早开始到最晚结束粗算，避免重复计数）。"""
    import re as _re

    spans = []
    for w in work or []:
        period = (w.get("period") or "").replace(" ", "")
        if not period:
            continue
        parts = _re.findall(r"(\d{4})[.\-/年]?(\d{1,2})?", period)
        starts = [p for p in parts if p[0]]
        if not starts:
            continue
        y0, m0 = starts[0]
        start = int(y0) * 12 + (int(m0) if m0 else 1)
        if "至今" in period or "现在" in period:
            import datetime as _dt
            now = _dt.date.today()
            end = now.year * 12 + now.month
        elif len(starts) >= 2:
            y1, m1 = starts[-1]
            end = int(y1) * 12 + (int(m1) if m1 else 1)
        else:
            end = start
        if end >= start:
            spans.append((start, end))
    if not spans:
        return None
    spans.sort()
    merged = [list(spans[0])]
    for s0, e0 in spans[1:]:
        if s0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e0)
        else:
            merged.append([s0, e0])
    return sum(e0 - s0 for s0, e0 in merged)

def work_count_of(row: dict) -> int:
    """工作经历条数：优先用字段，字段为空时按第N家槽位数推断。"""
    raw = str(row.get("工作经历条数", "") or "").strip()
    if raw.isdigit():
        return int(raw)
    slots = 0
    for i in range(1, 11):
        if (row.get(f"第{i}家公司") or "").strip() or (row.get(f"第{i}家起止") or "").strip():
            slots += 1
    return slots

def resume_from_row(row: dict) -> dict:
    """把一行 CSV 还原成 kb.evaluate 需要的简历结构。"""
    education = []
    school = (row.get("学校") or "").strip()
    degree = (row.get("学历") or "").strip()
    if school or degree:
        education.append({
            "school": school, "major": (row.get("专业") or "").strip(),
            "degree": degree, "mode": (row.get("学历性质") or "").strip(),
            "period": (row.get("教育起止") or "").strip(),
        })

    work = []
    for i in range(1, 11):
        company = (row.get(f"第{i}家公司") or "").strip()
        title = (row.get(f"第{i}家职位") or "").strip()
        period = (row.get(f"第{i}家起止") or "").strip()
        if company or period:
            work.append({"company": company, "title": title, "period": period,
                         "descriptions": []})

    reasons = [x for x in (row.get("复核原因") or "").split("；") if x]
    return {
        "education": education,
        "work": work,
        "collection": {
            "complete": (row.get("采集完成") or "") == "是",
            "work_count": work_count_of(row),
            "needs_review": (row.get("需复核") or "") == "是",
            "conflicts": [1] * int(row.get("冲突数") or 0),
            "review_reason": reasons,
        },
        "basic": {"age": row.get("年龄"), "education_mode": (row.get("学历性质") or ""),
                  "status": (row.get("求职状态") or "")},
        "gap_months": _last_job_gap_months(work),
        "total_work_months": _total_work_months(work),
        "company_1": (row.get("第1家公司") or ""),
    }


def evaluate_kb(row: dict, kb_data: dict | None = None) -> dict:
    """用知识库判硬规则与待复核。"""
    try:
        import kb as _kb
        data = kb_data or _kb.load()
        lookup = None
        try:
            import schools as _schools
            lookup = _schools.lookup
        except Exception:
            lookup = None
        return _kb.evaluate(resume_from_row(row), data, schools_lookup=lookup)
    except Exception as exc:
        return {"verdict": "", "reason": f"知识库不可用：{exc}", "failures": [],
                "reviews": [], "advisories": [], "exclusions": [],
                "education_status": "未知", "education_evidence": ""}


def _status_of_kb(kb_out: dict) -> str:
    """区分"业务判定"与"技术原因"：只有采集/字段缺失算无法核实。"""
    rules_hit = {f.get("rule") for f in (kb_out.get("failures") or [])}
    if "Q01" in rules_hit:
        return "无法核实"
    return "正常"


def _judge_work(row: dict) -> dict | None:
    """调用 DeepSeek 判工作经历；没有 key、没文本或失败时返回 None。"""
    try:
        import judge as _judge
        if not _judge.available():
            return None
        text = (row.get("工作经历全文") or row.get("全部工作经历") or "").strip()
        if not text:
            parts = []
            for i in range(1, 11):
                company = (row.get(f"第{i}家公司") or "").strip()
                title = (row.get(f"第{i}家职位") or "").strip()
                period = (row.get(f"第{i}家起止") or "").strip()
                if company or title:
                    parts.append(f"{period} {company}/{title}")
            text = "\n".join(parts)
        if not text:
            return None
        import kb as _kb
        res = _judge.judge(text, _kb.load())
        ok = res["recommendation"] == "建议合适"
        return {
            "最终结论": PASS if ok else REJECT,
            "处理状态": "正常",
            "原因": res.get("decision_basis", "")[:120],
            "经历结论": "匹配" if ok else "不匹配",
            "评分": "",
        }
    except Exception as exc:
        return {"最终结论": REJECT, "处理状态": "调用失败",
                "原因": f"判断模型调用失败：{exc}"[:120],
                "经历结论": "—", "评分": ""}


def screen_row(row: dict, rules: dict, scorer=None) -> dict:
    """对一条候选人记录按顺序判定：采集质量 -> 学历 -> 工作经历门槛 -> 评分。"""
    gate = screen_collection(row, rules)
    if not gate["collect_ok"]:
        # 二元口径：采集不全也要给出结论（按不合适收口，原因保留"采集中断"以便补采）
        return {
            "序号": row.get("序号", ""),
            "姓名": row.get("姓名", ""),
            "学校": row.get("学校", ""),
            "学历": row.get("学历", ""),
            "工作经历条数": row.get("工作经历条数", ""),
            "教育结论": "—",
            "教育原因": "—",
            "学校匹配": row.get("学校", ""),
            "经历结论": "—",
            "最终结论": REJECT,
            "处理状态": "采集不完整",
            "原因": f"采集不完整：{gate['collect_reason']}",
        }

    # 知识库硬规则（E01 学历 / E02 全日制 / W06 明确无经历）与待复核判定
    kb_out = evaluate_kb(row)
    if kb_out["verdict"] == "不合适":
        return {
            "序号": row.get("序号", ""),
            "姓名": row.get("姓名", ""),
            "学校": row.get("学校", ""),
            "学历": row.get("学历", ""),
            "工作经历条数": row.get("工作经历条数", ""),
            "教育结论": "不通过",
            "教育原因": kb_out["reason"],
            "学校匹配": row.get("学校", ""),
            "经历结论": "—",
            "最终结论": REJECT,
            "处理状态": _status_of_kb(kb_out),
            "原因": kb_out["reason"],
        }
    edu = screen_education(row, rules)
    out = {
        "序号": row.get("序号", ""),
        "姓名": row.get("姓名", ""),
        "学校": row.get("学校", ""),
        "学历": row.get("学历", ""),
        "工作经历条数": row.get("工作经历条数", ""),
        "教育结论": edu["edu_result"],
        "教育原因": edu["edu_reason"],
        "学校匹配": edu.get("school_matched", "") or row.get("学校", ""),
    }

    if edu["edu_result"] == REJECT:
        out.update({"经历结论": "—", "最终结论": REJECT,
                    "原因": edu["edu_reason"]})
        return out
    if edu["edu_result"] == REVIEW:
        # 二元口径：学历字段核实不了也算不合适（原因保留，便于补采）
        out.update({"经历结论": "—", "最终结论": REJECT,
                    "处理状态": "无法核实",
                    "原因": f"无法核实学历：{edu['edu_reason']}"})
        return out

    model_rules = rules.get("model", {})
    work = screen_work(row, rules, scorer,
                       threshold_pass=model_rules.get("threshold_pass"),
                       threshold_reject=model_rules.get("threshold_reject"))
    out.update({"经历结论": work["work_result"], "模型评分": work.get("score", "") or ""})

    if work["work_result"] == REJECT:
        out["最终结论"] = REJECT
        out["原因"] = f"{edu['edu_reason']}；{work['work_reason']}"
    elif work["work_result"] == "待评分":
        # 二元口径：学历通过后必须落到 合适/不合适
        decided = _judge_work(row)
        if decided:
            out.update(decided)
            out["原因"] = f"{edu['edu_reason']}；{decided['原因']}"
        else:
            out["最终结论"] = REJECT
            out["处理状态"] = "调用失败"
            out["原因"] = "未接入判断模型，无法确认工作经历匹配（默认按不合适）"
    else:
        out["最终结论"] = work["work_result"]
        out["原因"] = f"{edu['edu_reason']}；{work['work_reason']}"
    return out


def ensure_rules_file() -> Path:
    if not RULES_JSON.exists():
        DATA.mkdir(exist_ok=True)
        RULES_JSON.write_text(json.dumps(DEFAULT_RULES, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return RULES_JSON
