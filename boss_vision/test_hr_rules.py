"""人事规则（data\\hr-rules.json + 表格口径）的回归测试。

    python boss_vision\\test_hr_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

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


def test_basic_parse() -> None:
    print("[1] 简历顶部基本信息解析")
    got = vlm.normalize_basic({"basic": {"age": "26", "education_mode": "非全日制",
                                         "graduation_year": "2026", "status": "离职-随时到岗"}})
    check("年龄 26", got["age"] == 26, str(got["age"]))
    check("非全日制", got["education_mode"] == "非全日制", got["education_mode"])
    check("毕业年份 2026", got["graduation_year"] == 2026, str(got["graduation_year"]))
    check("求职状态", got["status"] == "离职-随时到岗", got["status"])

    got2 = vlm.normalize_basic({"basic": {"age": "", "education_mode": "全日制", "status": "应届生"}})
    check("空年龄 -> None", got2["age"] is None, str(got2["age"]))
    check("全日制保留", got2["education_mode"] == "全日制", got2["education_mode"])

    got3 = vlm.normalize_basic({"basic": {"education_mode": "函授"}})
    check("函授算非全日制", got3["education_mode"] == "非全日制", got3["education_mode"])


def test_hr_rules() -> None:
    print("\n[2] 人事硬条件（口径来自人事表）")
    rules = screening.load_rules()

    base = {"年龄": 26, "学历性质": "全日制", "求职状态": "离职-随时到岗",
            "第1家起止": "2024.01-至今"}

    ok = screening.screen_hr_rules(base, rules)
    check("全部符合 -> 通过", ok["hr_ok"], ok["hr_reason"])

    old = screening.screen_hr_rules(dict(base, 年龄=35), rules)
    check("35 岁超上限", not old["hr_ok"] and "年龄" in old["hr_reason"], old["hr_reason"])

    part = screening.screen_hr_rules(dict(base, 学历性质="非全日制"), rules)
    check("非全日制被拒", not part["hr_ok"] and "非全日制" in part["hr_reason"], part["hr_reason"])

    fresh = screening.screen_hr_rules(dict(base, 求职状态="应届生"), rules)
    check("应届生被拒", not fresh["hr_ok"] and "应届" in fresh["hr_reason"], fresh["hr_reason"])

    peer = screening.screen_hr_rules(dict(base, 第1家公司="福州一硕网络科技有限公司"), rules)
    check("同行被拒", not peer["hr_ok"] and "同行" in peer["hr_reason"], peer["hr_reason"])

    gap = screening.screen_hr_rules(dict(base, **{"第1家起止": "2023.01-2024.02"}), rules)
    check("空档期过长被拒", not gap["hr_ok"] and "空档" in gap["hr_reason"], gap["hr_reason"])

    print("\n[3] 接入整体判定")
    row = dict(base, **{"序号": 1, "姓名": "测试", "学校": "厦门大学", "学历": "本科",
                        "工作经历条数": 1, "已打开": "是", "采集完成": "是",
                        "读到章节": "work/edu", "需复核": "否", "复核原因": "", "冲突数": 0,
                        "年龄": 35})
    out = screening.screen_row(row, rules)
    check("超龄直接不合格", out["最终结论"] == screening.REJECT, out["最终结论"])
    check("原因写年龄", "年龄" in out["原因"], out["原因"])


def main() -> int:
    test_basic_parse()
    test_hr_rules()
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
