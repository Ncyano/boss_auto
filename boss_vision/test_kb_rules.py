"""知识库口径下的筛选回归测试。

重点：年龄/空档/同行/应届都**不得**用于淘汰（知识库 P01–P04、X01），
只有 E01（学历低于大专）、E02（非全日制）、W06（确认无工作经历）能直接判死。

    python boss_vision\\test_kb_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import kb as kbmod  # noqa: E402
import screening  # noqa: E402
import vlm  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def row_of(**kwargs) -> dict:
    base = {"序号": 1, "姓名": "测试", "学校": "厦门大学", "学历": "本科",
            "专业": "广告学", "学历性质": "全日制", "年龄": 24,
            "求职状态": "离职-随时到岗", "工作经历条数": 1,
            "采集完成": "是", "需复核": "否", "复核原因": "", "冲突数": 0,
            "第1家公司": "福州某电商有限公司", "第1家职位": "运营助理",
            "第1家起止": "2024.01-至今"}
    base.update(kwargs)
    return base


def test_basic() -> None:
    print("[1] 简历基本信息解析")
    got = vlm.normalize_basic({"basic": {"age": "26", "education_mode": "非全日制",
                                         "graduation_year": "2026", "status": "离职-随时到岗"}})
    check("年龄", got["age"] == 26, str(got["age"]))
    check("非全日制", got["education_mode"] == "非全日制")
    check("函授算非全日制",
          vlm.normalize_basic({"basic": {"education_mode": "函授"}})["education_mode"] == "非全日制")


def test_statuses() -> None:
    print("\n[2] 知识库规则状态")
    data = kbmod.load()
    check("E01 硬规则", kbmod.rule_kind(data, "E01") == "hard")
    check("E02 硬规则", kbmod.rule_kind(data, "E02") == "hard")
    check("W06 硬规则", kbmod.rule_kind(data, "W06") == "hard")
    check("W01 取证", kbmod.rule_kind(data, "W01") == "evidence")
    for rid in ("P01", "P02", "P03", "P04", "A01"):
        check(f"{rid} 是硬规则（v0.2）", kbmod.rule_kind(data, rid) == "hard")
    check("X01 已被 A01 取代", kbmod.rule_kind(data, "X01") == "superseded")
    check("版本 >= 0.2", str(data.get("version", "0")) >= "0.2", data.get("version"))


def test_rejections() -> None:
    print("\n[3] 能判死的（学历硬规则 / 无经历）")
    rules = screening.load_rules()

    low = screening.screen_row(row_of(学历="中专"), rules)
    check("中专 -> 不合格", low["最终结论"] == screening.REJECT, low["最终结论"])
    check("原因写明学历", "学历" in low["原因"], low["原因"])

    part = screening.screen_row(row_of(学历性质="非全日制"), rules)
    check("非全日制 -> 不合格", part["最终结论"] == screening.REJECT, part["最终结论"])

    none_work = screening.screen_row(row_of(工作经历条数=0), rules)
    check("无工作经历 -> 不合格", none_work["最终结论"] == screening.REJECT, none_work["最终结论"])


def test_new_hard_rules() -> None:
    print("\n[4] v0.2 硬规则：年龄/空档/同行/应届/经验 都要淘汰")
    rules = screening.load_rules()

    old = screening.screen_row(row_of(年龄=35), rules)
    check("35 岁 -> 不合格", old["最终结论"] == screening.REJECT, old["最终结论"])
    check("原因含 A01 年龄", "年龄" in old["原因"], old["原因"])

    gap = screening.screen_row(row_of(**{"第1家起止": "2022.01-2023.01"}), rules)
    check("空档超 6 个月 -> 不合格", gap["最终结论"] == screening.REJECT, gap["最终结论"])
    check("原因含空档", "空档" in gap["原因"], gap["原因"])

    peer = screening.screen_row(row_of(**{"第1家公司": "福州一硕网络科技有限公司"}), rules)
    check("同行 -> 不合格", peer["最终结论"] == screening.REJECT, peer["最终结论"])
    check("原因含同行", "同行" in peer["原因"], peer["原因"])

    fresh = screening.screen_row(row_of(求职状态="应届生"), rules)
    check("应届 -> 不合格", fresh["最终结论"] == screening.REJECT, fresh["最终结论"])
    check("原因含应届", "应届" in fresh["原因"], fresh["原因"])

    short = screening.screen_row(row_of(**{"第1家起止": "2026.08-2026.09"}), rules)
    check("经验不足 6 个月 -> 不合格", short["最终结论"] == screening.REJECT, short["最终结论"])
    check("原因含经验", "个月" in short["原因"], short["原因"])

    # 二元口径：工作经历模型未接入时也只能是"不合格"，但原因不能是硬规则命中
    ok = screening.screen_row(row_of(年龄=25, **{"第1家起止": "2023.01-至今"}), rules)
    check("25 岁 + 在职 -> 硬规则不命中",
          "年龄" not in ok["原因"] and "空档" not in ok["原因"], ok["原因"])


def test_review_paths() -> None:
    print("\n[5] 待复核")
    bad = screening.evaluate_kb(row_of(需复核="是", 复核原因="姓名未识别"))
    check("采集有问题 -> 不合格（二元口径）", bad["verdict"] == "不合适", bad["verdict"])

    unknown = screening.evaluate_kb(row_of(学历=""))
    check("学历未知 -> 不合格（二元口径）", unknown["verdict"] == "不合适", unknown["verdict"])

    ok = screening.evaluate_kb(row_of())
    check("全部合格 -> 交给工作证据层", ok["verdict"] == "", ok["verdict"])


def main() -> int:
    test_basic()
    test_statuses()
    test_rejections()
    test_new_hard_rules()
    test_review_paths()

    print("\n[6] 在校期间长期工作（P06）")
    data = kbmod.load()
    base = {"education": [{"school": "厦门大学", "degree": "本科", "period": "2020-2024",
                           "mode": "全日制"}],
            "collection": {"complete": True, "needs_review": False},
            "basic": {"age": 24, "education_mode": "全日制"}, "gap_months": 0,
            "total_work_months": 30}
    overlap = kbmod.evaluate({**base, "work": [{"company": "甲", "title": "客服",
                                                "period": "2022.03-2023.06",
                                                "descriptions": ["接待客户"]}]}, data)
    check("重合 15 个月 -> 不合适", overlap["verdict"] == "不合适", overlap["verdict"])
    check("命中 P06", any(f["rule"] == "P06" for f in overlap["failures"]),
          str([f["rule"] for f in overlap["failures"]]))

    intern = kbmod.evaluate({**base, "work": [{"company": "甲", "title": "客服实习",
                                               "period": "2022.03-2023.06",
                                               "descriptions": ["实习"]}]}, data)
    check("标了实习 -> 不判死", intern["verdict"] != "不合适", intern["verdict"])

    short = kbmod.evaluate({**base, "work": [{"company": "甲", "title": "客服",
                                              "period": "2024.01-2024.06",
                                              "descriptions": ["接待客户"]}]}, data)
    check("重合 5 个月 -> 不判死", short["verdict"] != "不合适", short["verdict"])

    print("\n[7] 姓名不影响判定")
    nameless = {"education": [{"school": "厦门大学", "degree": "本科", "period": "2020-2024",
                               "mode": "全日制"}],
                "work": [{"company": "甲", "title": "客服", "period": "2024.08-2025.06",
                          "descriptions": ["接待客户"]}],
                "collection": {"complete": True, "needs_review": False},
                "basic": {"age": 24, "education_mode": "全日制"}, "gap_months": 0,
                "total_work_months": 10}
    no_name = kbmod.evaluate(nameless, data)
    check("无姓名不影响结论", no_name["verdict"] != "不合适", no_name["verdict"])
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
