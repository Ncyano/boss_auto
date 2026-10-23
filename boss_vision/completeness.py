"""采集完整性检查：判定"这段简历是否已经采全"，并给出缺什么、要不要补采。

原则：
    · 缺字段 ≠ 业务结论，而是**补采信号**：先重读，补不回来才按不合适收口
    · 必填（缺了必须补）：姓名、学校、学历、每条工作的公司/职位/起止/职责
    · 建议填（缺了先补一次，补不回可接受）：年龄、学习形式、毕业年份、专业、教育起止

用法：
    from completeness import check
    ok, missing, retry_hint = check(resume_dict)
"""
from __future__ import annotations

import re

# 必填项：缺了必须补采
REQUIRED_EDU = ("school", "degree")
REQUIRED_WORK = ("company", "title", "descriptions")   # 起止缺失不阻断，只提示
# 建议项：尽量补
NICE_EDU = ("major", "period", "mode")
NICE_BASIC = ("age", "education_mode", "graduation_year")


def _blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return not str(value).strip()


def check(resume: dict) -> tuple[bool, list[str], list[str]]:
    """返回 (是否完整, 必填缺失项, 建议补充项)。

    resume 结构：
        {"name": str, "basic": {age, education_mode, graduation_year},
         "education": [{school, degree, major, period, mode}],
         "work": [{company, title, period, descriptions}],
         "collection": {"complete": bool, "sections": [...]}}
    """
    missing: list[str] = []
    nice: list[str] = []

    # 姓名不影响合适/不合适的判断，只作为"建议补全"（便于人工找人对号）
    if _blank(resume.get("name")):
        nice.append("姓名")

    education = resume.get("education") or []
    work = resume.get("work") or []

    if not education:
        missing.append("教育经历")
    else:
        first = education[0]
        for key in REQUIRED_EDU:
            if _blank(first.get(key)):
                missing.append({"school": "学校名", "degree": "学历"}.get(key, key))
        for key in NICE_EDU:
            if _blank(first.get(key)):
                nice.append({"major": "专业", "period": "教育起止", "mode": "学习形式"}.get(key, key))

    if not work:
        # 没有工作经历本身可能是事实，但要看采集是否读到章节
        collection = resume.get("collection") or {}
        if not collection.get("complete"):
            missing.append("工作经历（未读到章节）")
    else:
        for idx, item in enumerate(work, 1):
            kind = str(item.get("section") or "work").lower()
            if kind in ("project", "campus", "other"):
                continue          # 项目/校园经历不要求公司名
            # 公司名与起止都空 -> 多半是标签行（"商家运营 | 线上运营"），不是真工作
            if _blank(item.get("company")) and _blank(item.get("period")):
                continue
            if _blank(item.get("period")):
                nice.append(f"第{idx}家起止")
            for key in REQUIRED_WORK:
                if key == "descriptions":
                    if _blank(item.get("descriptions")):
                        missing.append(f"第{idx}家职责")
                elif _blank(item.get(key)):
                    label = {"company": "公司名", "title": "职位", "period": "起止"}[key]
                    missing.append(f"第{idx}家{label}")

    basic = resume.get("basic") or {}
    for key in NICE_BASIC:
        if _blank(basic.get(key)):
            nice.append({"age": "年龄", "education_mode": "学习形式",
                         "graduation_year": "毕业年份"}.get(key, key))

    return (not missing), missing, nice


def retry_hints(missing: list[str]) -> list[str]:
    """根据缺失项给出补采动作建议。"""
    hints: list[str] = []
    joined = " ".join(missing)
    if any(k in joined for k in ("学校", "学历", "专业", "教育")):
        hints.append("继续向下滚动：教育经历区可能还没进入视野")
    if "职责" in joined:
        hints.append("重读当前屏：职责可能被截断，需放大后重读")
    if any(k in joined for k in ("公司名", "职位", "起止")):
        hints.append("向上回滚半屏：跨屏边界可能漏了经历的头一行")
    if any(k in joined for k in ("姓名", "年龄", "学习形式")):
        hints.append("回到顶部重读：头部基本信息区可能未进入视野")
    return hints


def merge_missing(*lists: list[str]) -> list[str]:
    """合并多屏的缺失项，去重保序。"""
    out: list[str] = []
    for items in lists:
        for item in items or []:
            if item and item not in out:
                out.append(item)
    return out


def summarize(missing: list[str]) -> str:
    return "；".join(missing[:6]) + ("…" if len(missing) > 6 else "")


def self_test() -> int:
    ok = fail = 0

    def check_(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    full = {
        "name": "张三",
        "basic": {"age": 25, "education_mode": "全日制", "graduation_year": 2023},
        "education": [{"school": "厦门大学", "degree": "本科", "major": "广告学",
                       "period": "2019-2023", "mode": "全日制"}],
        "work": [{"company": "甲公司", "title": "客服", "period": "2023.07-至今",
                  "descriptions": ["负责客户咨询"]}],
        "collection": {"complete": True},
    }
    good, missing, nice = check(full)
    check_("完整简历判定为完整", good and not missing, str(missing))
    check_("完整简历没有建议项", not nice, str(nice))

    print("\n[2] 缺失检测")
    no_school = dict(full, education=[{"school": "", "degree": "本科", "major": "广告学"}])
    good2, miss2, _ = check(no_school)
    check_("缺学校 -> 不完整", not good2 and "学校名" in miss2, str(miss2))

    no_degree = dict(full, education=[{"school": "厦门大学", "degree": ""}])
    _, miss3, _ = check(no_degree)
    check_("缺学历 -> 不完整", "学历" in miss3, str(miss3))

    no_duty = dict(full, work=[{"company": "甲公司", "title": "客服",
                                "period": "2023.07-至今", "descriptions": []}])
    _, miss4, _ = check(no_duty)
    check_("缺职责 -> 不完整", "第1家职责" in miss4, str(miss4))

    no_company = dict(full, work=[{"company": "", "title": "客服", "period": "2023.07",
                                   "descriptions": ["做事"]}])
    _, miss5, _ = check(no_company)
    check_("缺公司名 -> 不完整", "第1家公司名" in miss5, str(miss5))

    no_age = {k: v for k, v in full.items() if k != "basic"}
    good6, miss6, nice6 = check(no_age)
    check_("缺年龄 -> 建议项不是必填", good6 and "年龄" in nice6, f"{miss6} / {nice6}")

    print("\n[3] 补采建议")
    hints = retry_hints(["学校名", "第1家职责"])
    check_("缺学校 -> 向下滚", any("向下滚动" in h for h in hints), str(hints))
    check_("缺职责 -> 重读当前屏", any("重读当前屏" in h for h in hints), str(hints))
    check_("合并去重", merge_missing(["a", "b"], ["b", "c"]) == ["a", "b", "c"])

    print(f"\n{ok}/{ok + fail} 通过")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
