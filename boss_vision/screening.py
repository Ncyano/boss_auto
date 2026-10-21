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
REJECT = "不合格"
REVIEW = "待复核"


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
    """把各段工作经历拼成模型输入文本（职位 + 时间 + 职责，公司名不进特征）。"""
    parts: list[str] = []
    for i in range(1, 11):
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


def screen_row(row: dict, rules: dict, scorer=None) -> dict:
    """对一条候选人记录按顺序判定：学历 -> 工作经历门槛 -> 评分。"""
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
        out.update({"经历结论": "—", "最终结论": REVIEW, "原因": edu["edu_reason"]})
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
        out["最终结论"] = REVIEW
        out["原因"] = f"{edu['edu_reason']}；{work['work_reason']}"
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
