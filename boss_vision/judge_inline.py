"""采集过程中即时判定：学历硬规则 → 通过则马上做"合适/不合适"二分类。

不点击打招呼，只出结论。判定逻辑复用 screening（内含知识库硬规则 + DeepSeek 工作经历判断）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SLOTS = 10


def record_to_row(rec: dict) -> dict:
    """把 driver 的 record 映射成 screening 认识的一行。"""
    basic = rec.get("basic") or {}
    education = rec.get("education") or []
    work = rec.get("work") or []
    first = education[0] if education else {}

    row = {
        "序号": rec.get("index", ""),
        "姓名": rec.get("name", ""),
        "年龄": basic.get("age") or "",
        "学历性质": basic.get("education_mode") or "",
        "毕业年份": basic.get("graduation_year") or "",
        "求职状态": basic.get("status") or "",
        "学校": (first.get("school") or "").strip(),
        "专业": (first.get("major") or "").strip(),
        "学历": (first.get("degree") or "").strip(),
        "教育起止": (first.get("period") or "").strip(),
        "工作经历条数": len(work),
        "采集完成": "是" if rec.get("complete") or rec.get("reached_education") else "否",
        "需复核": "是" if rec.get("needs_review") else "否",
        "复核原因": rec.get("review_reason") or "",
        "冲突数": len(rec.get("conflicts") or []),
        "缺失字段": "；".join(rec.get("missing") or []),
        "工作经历状态": rec.get("work_state", ""),
        "工作经历全文": "\n".join(
            f'{w.get("period", "")} {w.get("company", "")}/{w.get("title", "")}'
            + ("：" + "；".join(w.get("descriptions") or []) if w.get("descriptions") else "")
            for w in work),
    }
    for i in range(1, SLOTS + 1):
        w = work[i - 1] if i - 1 < len(work) else {}
        row[f"第{i}家公司"] = w.get("company", "")
        row[f"第{i}家职位"] = w.get("title", "")
        row[f"第{i}家起止"] = w.get("period", "")
    return row


def judge_record(rec: dict, rules: dict | None = None) -> dict:
    """返回 {"verdict": 合适/不合适, "reason": str, "edu": str, "work": str, "stage": str}。"""
    import screening

    rules = rules or screening.load_rules()
    row = record_to_row(rec)
    try:
        out = screening.screen_row(row, rules)
    except Exception as exc:
        return {"verdict": "不合适", "status": "调用失败", "reason": f"判定失败：{exc}",
                "edu": "—", "work": "—", "stage": "error"}

    verdict = out.get("最终结论") or "不合适"
    status = out.get("处理状态") or "正常"
    reason = (out.get("原因") or "").strip()
    edu = out.get("教育结论") or "—"
    work = out.get("经历结论") or "—"
    reason_text = f"{out.get('原因') or ''}"
    if "采集" in reason_text or "缺失" in reason_text:
        stage = "采集"
    elif work in ("—", "", None):
        stage = "硬规则"      # 学历/年龄/空档/同行/应届/经验，都属硬规则拦截
    else:
        stage = "工作经历"
    return {"verdict": verdict, "status": status, "reason": reason,
            "edu": edu, "work": work, "stage": stage}


def self_test() -> int:
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    print("[1] record -> row 映射")
    rec = {
        "index": 3, "name": "张三",
        "basic": {"age": 24, "education_mode": "全日制", "status": "离职-随时到岗"},
        "education": [{"school": "厦门大学", "major": "广告学", "degree": "本科",
                       "period": "2020-2024"}],
        "work": [{"company": "甲公司", "title": "客服", "period": "2024.01-至今",
                  "descriptions": ["负责客户咨询"]}],
        "complete": True, "reached_education": True, "needs_review": False,
        "conflicts": [], "missing": [],
    }
    row = record_to_row(rec)
    check("姓名", row["姓名"] == "张三")
    check("学校", row["学校"] == "厦门大学")
    check("工作条数", row["工作经历条数"] == 1, str(row["工作经历条数"]))
    check("第1家公司", row["第1家公司"] == "甲公司")
    check("经历全文含职责", "负责客户咨询" in row["工作经历全文"])

    print("\n[2] 判定（学历层拦截，不需要模型）")
    bad = dict(rec, education=[{"school": "某中专", "degree": "中专"}])
    out = judge_record(bad)
    check("中专 -> 不合适", out["verdict"] == "不合适", out["verdict"])
    check("阶段=硬规则", out["stage"] == "硬规则", out["stage"])
    check("原因含学历", "学历" in out["reason"], out["reason"])

    no_work = dict(rec, work=[])
    out2 = judge_record(no_work)
    check("无工作经历 -> 不合适", out2["verdict"] == "不合适", out2["verdict"])

    print("\n[3] 采集缺失 -> 不合适（二元口径）")
    gap = dict(rec, missing=["第1家职责"])
    out3 = judge_record(gap)
    check("缺职责 -> 不合适", out3["verdict"] == "不合适", out3["verdict"])
    check("原因写采集缺失", "采集" in out3["reason"], out3["reason"])

    print(f"\n{ok}/{ok + fail} 通过")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
