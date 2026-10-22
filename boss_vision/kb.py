"""运营助理知识库加载与执行。

以 `运营助理知识库_v0.1/rules.json` 为准，规则状态决定怎么用：

    enabled / confirmed_by_user          可执行（硬规则：命中即不合适）
    confirmed_policy_pending_reference   政策已确认但依据未接入 -> 只能判"待复核"
    draft_from_hr                        工作证据（交给模型逐条取证，不单独淘汰）
    disabled_pending_confirmation        阈值待确认 -> 只提示，不得淘汰
    excluded                             完全不作判定依据（年龄/性格/人事标签）

关键约束（来自知识库）：
    · 年龄不能作为筛选特征（X01）
    · 空档期、同行、在校实习、半年经验都是"待确认"，只能提示（P01–P04）
    · 未命中学校名单 ≠ 学历造假，只能待复核（E03）
    · 未提及 ≠ 不存在；无法判断 ≠ 不满足（W05）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "运营助理知识库_v0.1"

HARD_STATUSES = {"enabled", "confirmed_by_user"}
PENDING_REF_STATUSES = {"confirmed_policy_pending_reference"}
DRAFT_STATUSES = {"draft_from_hr"}
DISABLED_STATUSES = {"disabled_pending_confirmation"}
EXCLUDED_STATUSES = {"excluded"}
SUPERSEDED_STATUSES = {"superseded"}

DEGREE_ORDER = {"初中及以下": 0, "高中": 1, "中专": 1, "中技": 1, "中专/中技": 1,
                "大专": 2, "专科": 2, "本科": 3, "硕士": 4, "研究生": 4, "博士": 5}


def load(kb_dir: Path | None = None) -> dict:
    folder = kb_dir or KB_DIR
    path = folder / "rules.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到知识库：{path}")
    kb = json.loads(path.read_text(encoding="utf-8"))
    kb["_dir"] = str(folder)
    kb["_rules"] = {r["id"]: r for r in kb.get("rules", [])}
    return kb


def rule_status(kb: dict, rule_id: str) -> str:
    return (kb["_rules"].get(rule_id) or {}).get("status", "")


def rule_kind(kb: dict, rule_id: str) -> str:
    """把状态映射成使用方式。"""
    st = rule_status(kb, rule_id)
    if st in HARD_STATUSES:
        return "hard"
    if st in PENDING_REF_STATUSES:
        return "review"
    if st in DRAFT_STATUSES:
        return "evidence"
    if st in DISABLED_STATUSES:
        return "advisory"
    if st in EXCLUDED_STATUSES:
        return "excluded"
    if st in SUPERSEDED_STATUSES:
        return "superseded"
    return "unknown"


def enabled_work_rules(kb: dict) -> list[dict]:
    """W01–W05 这类工作证据规则（交给模型逐条取证）。"""
    return [r for r in kb.get("rules", []) if rule_kind(kb, r["id"]) == "evidence"]


def params_of(kb: dict, rule_id: str) -> dict:
    return (kb["_rules"].get(rule_id) or {}).get("params") or {}


def hard_rejections(kb: dict) -> list[str]:
    """当前所有会直接判死的规则编号（便于日志与文档）。"""
    return [r["id"] for r in kb.get("rules", [])
            if rule_kind(kb, r["id"]) in ("hard", "review") and r.get("kind") == "hard"]


def degree_level(text: str) -> int:
    t = (text or "").strip()
    for key in ("博士", "硕士", "研究生", "本科", "大专", "专科", "中专", "中技", "高中", "初中"):
        if key in t:
            return DEGREE_ORDER.get(key, 0)
    return -1


_SEP_RE = re.compile(r"\s*[-–—~至到]+\s*")
_POINT_RE = re.compile(r"^(\d{4})(?:[.\-/年]?(\d{1,2}))?月?$")


def _month_index(year: int, month: int) -> int:
    return year * 12 + month


def _parse_point(text: str, default_month: int) -> int | None:
    """'2022' / '2022.03' / '2022年3月' -> 月序号。"""
    t = (text or "").strip().replace("月", "")
    if not t:
        return None
    m = _POINT_RE.match(t)
    if not m:
        # 兜底：取出前 4 位年份
        m2 = re.match(r"^(\d{4})", t)
        if not m2:
            return None
        return _month_index(int(m2.group(1)), default_month)
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else default_month
    if not (1 <= month <= 12):
        month = default_month
    return _month_index(year, month)


def _span(period: str, start_month: int = 1, end_month: int = 12,
          is_edu: bool = False) -> tuple[int, int] | None:
    """把起止解析成 (起始月序号, 结束月序号)。"""
    text = (period or "").replace(" ", "")
    if not text:
        return None

    begin_default = start_month if is_edu else 1
    end_default = end_month if is_edu else 12

    if "至今" in text or "现在" in text:
        import datetime as _dt
        head = _SEP_RE.split(text.split("至今")[0].split("现在")[0])[0]
        start = _parse_point(head, begin_default)
        now = _dt.date.today()
        return (start if start is not None else _month_index(now.year, now.month),
                _month_index(now.year, now.month))

    halves = [x for x in _SEP_RE.split(text) if x]
    if not halves:
        return None
    start = _parse_point(halves[0], begin_default)
    if start is None:
        return None
    if len(halves) >= 2:
        end = _parse_point(halves[-1], end_default)
    else:
        end = start + (10 if is_edu else 0)
    if end is None:
        end = start
    if end < start:
        end = start
    return start, end


def overlap_months(edu_period: str, work_period: str) -> int:
    """教育时间段与某段工作的重合月数（教育按 9 月入学 / 6 月毕业）。"""
    edu = _span(edu_period, start_month=9, end_month=6, is_edu=True)
    work = _span(work_period)
    if not edu or not work:
        return 0
    return max(0, min(edu[1], work[1]) - max(edu[0], work[0]))


_INTERN_MARKERS = ("实习", "兼职", "勤工", "实训", "见习", "part-time", "intern",
                   "助教", "家教", "代课", "辅导员助理", "校园大使", "学生会", "社团")


def _is_intern(entry: dict, markers: list[str]) -> bool:
    text = " ".join([str(entry.get("title") or "")] +
                    [str(x) for x in (entry.get("descriptions") or [])])
    low = text.lower()
    return any(m.lower() in low for m in markers)


def _check_school_work_overlap(education: list, work: list, params: dict) -> list[dict]:
    """在校期间工作：重合 ≥ N 个月且没有实习标记 -> 不合格。"""
    if not education or not work:
        return []
    min_overlap = int(params.get("min_overlap_months", 12))
    markers = params.get("intern_markers") or list(_INTERN_MARKERS)
    edu_period = (education[0].get("period") or "")
    if not edu_period:
        return []
    problems = []
    for idx, entry in enumerate(work, 1):
        months = overlap_months(edu_period, entry.get("period") or "")
        if months >= min_overlap and not _is_intern(entry, markers):
            problems.append({
                "rule": "P06",
                "reason": f"在校期间工作 {months} 个月（第{idx}家）："
                          f"{entry.get('company') or ''} {entry.get('title') or ''}",
                "evidence": f"教育 {edu_period} / 工作 {entry.get('period')}",
            })
            break
    return problems

def _norm_company(text: str) -> str:
    """公司名归一：去空格、全角括号转半角、去掉末尾标点。"""
    t = (text or "").strip()
    t = t.replace("（", "(").replace("）", ")").replace("　", "")
    t = re.sub(r"\s+", "", t)
    return t.rstrip("。.;；,，")


def _similarity(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _is_peer_company(company: str, params: dict) -> bool:
    """同行判定：品牌词命中 + 与对应全称足够相似。

    · 只用品牌词（云上源/一硕/谷一~三/南风网络/正北传媒/飞云好物/天成卓越），
      不用「网络」「科技」这类通用词，避免误伤无关公司
    · 品牌词 ≥4 字：命中即可（如「飞云好物」「天成卓越」）
    · 品牌词 2~3 字：还要求与全称相似度 ≥ min_ratio（默认 0.45）
    """
    name = _norm_company(company)
    if not name:
        return False
    brands = params.get("brands") or []
    min_ratio = float(params.get("min_ratio", 0.45))

    for item in brands:
        if isinstance(item, str):
            token, full = item, item
        else:
            token = str(item.get("token") or "")
            full = str(item.get("full") or token)
        if not token or token not in name:
            continue
        if len(token) >= 4:
            return True
        if _similarity(name, _norm_company(full)) >= min_ratio:
            return True
    return False


def _title_hits(entry: dict, titles: list[str]) -> list[str]:
    """职位/职责里命中的岗位词（取最长匹配，避免"客服"吞掉"售前客服"）。"""
    text = " ".join([str(entry.get("title") or "")] +
                    [str(x) for x in (entry.get("descriptions") or [])])
    hits = [t for t in titles if t and t in text]
    return sorted(hits, key=len, reverse=True)


def _check_job_profile(work: list, params: dict) -> tuple[list[dict], list[str]]:
    """按人才画像判岗位偏差。

    返回 (是否触发 NP01 的失败项, 命中的优先岗位词)。
    规则：所有经历都在排除清单里、且没有任何一条命中优先清单 -> 不合适。
    """
    exclude = params.get("exclude_titles") or []
    prefer = params.get("prefer_titles") or []
    if not work:
        return [], []

    prefer_hits: list[str] = []
    exclude_hits: list[str] = []
    unclassified = 0
    for entry in work:
        ph = _title_hits(entry, prefer)
        if ph:
            prefer_hits.extend(ph)
            continue
        eh = _title_hits(entry, exclude)
        if eh:
            exclude_hits.extend(eh)
        else:
            unclassified += 1

    if prefer_hits:
        return [], prefer_hits          # 有优先岗位经历 -> 正向，不判死

    # 没有任何优先岗位：如果每段都能归到排除清单（没有"未分类"经历），判不合适
    if exclude_hits and unclassified == 0:
        top = sorted(set(exclude_hits), key=len, reverse=True)[:3]
        return [{"rule": "NP01",
                 "reason": f"工作经历均为排除岗位（{'、'.join(top)}），无沟通/销售/运营类经验",
                 "evidence": "、".join(sorted(set(exclude_hits))[:4])}], []
    return [], []

def evaluate(resume: dict, kb: dict | None = None, schools_lookup=None) -> dict:
    """按知识库执行硬规则，返回判定与全部提示。

    resume 需要的字段：
        education: [{school, major, degree, period, mode?}]
        work:      [{company, title, period, descriptions}]
        collection: {opened, complete, conflicts, needs_review}
        basic:     {age, education_mode, status}
    """
    kb = kb or load()
    education = resume.get("education") or []
    work = resume.get("work") or []
    collection = resume.get("collection") or {}
    basic = resume.get("basic") or {}

    failures: list[dict] = []      # 判死
    reviews: list[str] = []        # 待复核
    advisories: list[str] = []     # 只提示
    exclusions: list[str] = []     # 按知识库不得使用，仅记录

    # ---------- Q01 采集质量 ----------
    if collection.get("needs_review") or collection.get("conflicts"):
        reviews.append("采集待复核：" + "；".join(
            collection.get("review_reason") or [f"{len(collection.get('conflicts') or [])} 处冲突"]))

    # ---------- 教育 ----------
    first = education[0] if education else None
    if first is None:
        reviews.append("未读到教育经历，无法核对学历硬规则")
        education_status = "未知"
        education_evidence = ""
    else:
        degree_text = (first.get("degree") or "").strip()
        level = degree_level(degree_text)
        education_evidence = json.dumps(
            {"school": first.get("school"), "degree": degree_text,
             "period": first.get("period")}, ensure_ascii=False)

        if level < 0:
            reviews.append("第一段教育学历层次未识别（P05：多段学历口径未确认）")
            education_status = "未知"
        elif level < DEGREE_ORDER["大专"]:
            education_status = "不通过"
            failures.append({"rule": "E01", "reason": f"学历为{degree_text}，低于大专",
                             "evidence": education_evidence})
        else:
            education_status = "通过"

        # E02 非全日制
        mode = (first.get("mode") or basic.get("education_mode") or "").strip()
        if "非全日制" in mode:
            education_status = "不通过"
            failures.append({"rule": "E02", "reason": "学历为非全日制", "evidence": education_evidence})

        # E02 补充：学习形式未知时无法排除非全日制 -> 待复核（未知不等于通过）
        if education_status == "通过":
            mode_now = (first.get("mode") or basic.get("education_mode") or "").strip()
            if not mode_now:
                reviews.append("学习形式未知（未读到全日制/非全日制），E02 无法核对")

        # E03 学校名单：政策已确认，但依据未接入 -> 未命中只能待复核
        school = (first.get("school") or "").strip()
        if schools_lookup is not None and school and education_status == "通过":
            try:
                hit = schools_lookup(school)
                if not hit.get("listed"):
                    failures.append({"rule": "E03",
                                     "reason": f"校名 {school} 不在教育部名单",
                                     "evidence": f"学校：{school}"})
            except Exception:
                pass

    # ---------- W06 明确没有工作经历 ----------
    if not work and not collection.get("needs_review"):
        count = collection.get("work_count")
        try:
            count_int = int(count) if count not in (None, "") else None
        except (TypeError, ValueError):
            count_int = None
        if count_int is not None and count_int > 0:
            # 简历说有 N 段经历，但我们没拿到明细 -> 采集不全，不是"没有经历"
            reviews.append(f"简历标注 {count_int} 段工作经历，但未取到明细")
        elif collection.get("complete") or count_int == 0:
            failures.append({"rule": "W06", "reason": "确认无工作经历",
                             "evidence": "简历完整读取，工作经历为空"})

    # ---------- v0.2.0：年龄 / 空档 / 同行 / 应届 / 经验 都是硬性不合适 ----------
    age = basic.get("age")
    age_max = params_of(kb, "A01").get("max_age", 30)
    if age:
        try:
            age_int = int(age)
        except (TypeError, ValueError):
            age_int = 0
        if age_int > int(age_max):
            failures.append({"rule": "A01", "reason": f"年龄 {age_int} 超过上限 {age_max}",
                             "evidence": f"简历年龄：{age_int}"})
        else:
            advisories.append(f"年龄 {age_int}（未超上限 {age_max}）")
    else:
        reviews.append("年龄未识别，无法执行 A01 年龄上限（未知不等于通过）")

    gap = resume.get("gap_months")
    gap_max = params_of(kb, "P02").get("max_gap_months", 6)
    if gap is not None:
        if gap > int(gap_max):
            failures.append({"rule": "P02", "reason": f"最近空档 {gap} 个月，超过 {gap_max} 个月",
                             "evidence": f"最近一段工作结束至今 {gap} 个月"})
        else:
            advisories.append(f"最近空档 {gap} 个月（上限 {gap_max}）")

    p03 = params_of(kb, "P03") or {}
    companies = [(w.get("company") or "").strip() for w in work]
    if not any(companies):            # 兼容旧接口：只给了 company_N
        companies = [resume.get(f"company_{i}") or "" for i in range(1, 11)]
    for company in companies:
        if not company:
            continue
        if _is_peer_company(company, p03):
            failures.append({"rule": "P03", "reason": f"同行公司：{company}",
                             "evidence": company})
            break

    status_text = (basic.get("status") or "")
    fresh_keys = params_of(kb, "P04").get("keywords") or ["在校", "应届", "实习"]
    hit_word = next((k for k in fresh_keys if k in status_text), "")
    if hit_word:
        failures.append({"rule": "P04", "reason": f"求职状态含「{hit_word}」（{status_text}）",
                         "evidence": status_text})

    # NP01 / W07：按人事人才画像判岗位偏差
    profile_fail, prefer_hits = _check_job_profile(work, {
        **(params_of(kb, "NP01") or {}),
        "prefer_titles": (params_of(kb, "NP01") or {}).get("prefer_titles")
                         or (params_of(kb, "W07") or {}).get("prefer_titles") or [],
    })
    failures.extend(profile_fail)
    if prefer_hits:
        advisories.append(f"命中优先岗位：{'、'.join(sorted(set(prefer_hits))[:3])}")

    # P06：在校期间长期工作（无实习标记）
    failures.extend(_check_school_work_overlap(education, work, params_of(kb, "P06")))

    months = resume.get("total_work_months")
    min_months = params_of(kb, "P01").get("min_months", 6)
    if months is not None:
        if months < int(min_months):
            failures.append({"rule": "P01", "reason": f"累计相关工作 {months} 个月，不足 {min_months} 个月",
                             "evidence": f"累计工作月数 {months}"})
        else:
            advisories.append(f"累计工作 {months} 个月（门槛 {min_months}）")

    # ---------- 结论（二元：只有 合适 / 不合适） ----------
    # 知识库口径：核实不了（采集缺失/学历未知/学校未核实）不能算通过，
    # 在二元模式下按"不合适"处理，并保留原因。
    if reviews:
        for text in reviews:
            failures.append({"rule": "Q01", "reason": f"无法核实：{text}",
                             "evidence": "资料缺失"})

    if failures:
        verdict = "不合适"
    elif education_status == "通过":
        verdict = ""          # 交给工作证据模型（W01–W05）
    else:
        failures.append({"rule": "E01", "reason": "学历状态未知，无法核实",
                         "evidence": education_evidence or "无教育记录"})
        verdict = "不合适"

    return {
        "verdict": verdict,
        "education_status": education_status,
        "education_evidence": education_evidence,
        "failures": failures,
        "reviews": reviews,
        "advisories": advisories,
        "exclusions": exclusions,
        "kb_version": kb.get("version", ""),
        "reason": "；".join(f["reason"] for f in failures),
    }


def self_test() -> int:
    kb = load()
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    print(f"[1] 加载知识库 v{kb.get('version')}，{len(kb.get('rules', []))} 条规则")
    check("E01 是硬规则", rule_kind(kb, "E01") == "hard")
    check("E02 是硬规则", rule_kind(kb, "E02") == "hard")
    check("W06 是硬规则", rule_kind(kb, "W06") == "hard")
    check("W01 是取证规则", rule_kind(kb, "W01") == "evidence")
    for rid in ("P01", "P02", "P03", "P04", "A01"):
        check(f"{rid} 硬规则（v0.2）", rule_kind(kb, rid) == "hard")
    check("X01 已被 A01 取代", rule_kind(kb, "X01") == "superseded")
    check("E03 只能待复核", rule_kind(kb, "E03") == "review")

    print("\n[2] 判定")
    base = {"education": [{"school": "厦门大学", "degree": "本科", "period": "2020-2024",
                           "mode": "全日制"}],
            "work": [{"company": "甲公司", "title": "客服", "period": "2024.01-至今"}],
            "collection": {"complete": True, "needs_review": False},
            "basic": {"age": 25, "education_mode": "全日制"}, "gap_months": 0,
            "total_work_months": 20, "company_1": "甲公司"}

    out = evaluate(dict(base), kb)
    check("正常画像留给工作模型", out["verdict"] == "", out["verdict"] or "（空=交给模型）")

    aged = evaluate({**base, "basic": {"age": 35}}, kb)
    check("35 岁 -> 不合适（A01）", aged["verdict"] == "不合适", aged["verdict"])
    check("命中 A01", any(f["rule"] == "A01" for f in aged["failures"]), str(aged["failures"]))

    low = evaluate({**base, "education": [{"school": "某中职", "degree": "中专"}]}, kb)
    check("中专 -> 不合适", low["verdict"] == "不合适", low["verdict"])
    check("命中 E01", any(f["rule"] == "E01" for f in low["failures"]))

    part = evaluate({**base, "education": [{"school": "某学院", "degree": "本科",
                                            "mode": "非全日制"}]}, kb)
    check("非全日制 -> 不合适", part["verdict"] == "不合适", part["verdict"])
    check("命中 E02", any(f["rule"] == "E02" for f in part["failures"]))

    none_work = evaluate({**base, "work": []}, kb)
    check("确认无经历 -> 不合适", none_work["verdict"] == "不合适", none_work["verdict"])
    check("命中 W06", any(f["rule"] == "W06" for f in none_work["failures"]))

    review = evaluate({**base, "collection": {"complete": False, "needs_review": True,
                                              "review_reason": ["姓名未识别"]}}, kb)
    check("采集有问题 -> 不合格（二元口径）", review["verdict"] == "不合适", review["verdict"])

    gap = evaluate({**base, "gap_months": 20}, kb)
    check("空档超 6 个月 -> 不合适（P02）", gap["verdict"] == "不合适", gap["verdict"])
    check("命中 P02", any(f["rule"] == "P02" for f in gap["failures"]))

    peer = evaluate({**base, "company_1": "福州一硕网络科技有限公司"}, kb)
    check("同行 -> 不合适（P03）", peer["verdict"] == "不合适", peer["verdict"])
    check("命中 P03", any(f["rule"] == "P03" for f in peer["failures"]))

    fresh = evaluate({**base, "basic": {"age": 25, "status": "应届生"}}, kb)
    check("应届 -> 不合适（P04）", fresh["verdict"] == "不合适", fresh["verdict"])
    check("命中 P04", any(f["rule"] == "P04" for f in fresh["failures"]))

    short = evaluate({**base, "total_work_months": 3}, kb)
    check("经验不足 6 个月 -> 不合适（P01）", short["verdict"] == "不合适", short["verdict"])
    check("命中 P01", any(f["rule"] == "P01" for f in short["failures"]))

    noage = evaluate({**base, "basic": {}}, kb)
    check("年龄未知 -> 不合格（二元口径）", noage["verdict"] == "不合适", noage["verdict"])

    print(f"\n{ok}/{ok + fail} 通过")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
