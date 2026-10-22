"""离线筛选规则的自测：python boss_vision\\test_screening.py"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import screening  # noqa: E402

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


def row(**kw) -> dict:
    # 默认给一个可通过 v0.2 硬规则的画像：25 岁、全日制、非应届、近期在职
    base = {"序号": 1, "姓名": "测试", "学校": "", "学历": "", "工作经历条数": 0,
            "年龄": 25, "学历性质": "全日制", "求职状态": "离职-随时到岗",
            "采集完成": "是", "需复核": "否", "复核原因": "", "冲突数": 0}
    base.update(kw)
    return base


def main() -> int:
    rules = screening.load_rules()

    print("[1] 学历层次折算")
    check("本科=6", screening.degree_level("本科") == 6)
    check("大专=5", screening.degree_level("大专") == 5)
    check("中专/中技=4", screening.degree_level("中专/中技") == 4)
    check("本科（非全日制）仍识别为本科",
          screening.degree_level("本科（非全日制）") == 6)
    check("空文本为 None", screening.degree_level("") is None)

    print("\n[2] 教育规则")
    ok = screening.screen_education(row(学校="厦门大学", 学历="本科"), rules)
    check("名单内 + 本科 -> 通过", ok["edu_result"] == screening.PASS, ok["edu_reason"])

    bad = screening.screen_education(row(学校="某某职业技术学院", 学历="本科"), rules)
    check("名单外 -> 不合格", bad["edu_result"] == screening.REJECT, bad["edu_reason"])

    low = screening.screen_education(row(学校="厦门大学", 学历="中专"), rules)
    check("学历低于门槛 -> 不合格", low["edu_result"] == screening.REJECT, low["edu_reason"])

    alias = screening.screen_education(row(学校="福州职业技术学院", 学历="本科"), rules)
    check("曾用名命中且记录说明", alias["edu_result"] == screening.PASS
          and "曾用名" in alias["edu_reason"], alias["edu_reason"])

    no_deg = screening.screen_education(row(学校="厦门大学", 学历=""), rules)
    check("缺学历 -> 待复核", no_deg["edu_result"] == screening.REVIEW, no_deg["edu_reason"])

    no_school = screening.screen_education(row(学校="", 学历="本科"), rules)
    check("缺学校 -> 待复核（不当成名单外）", no_school["edu_result"] == screening.REVIEW,
          no_school["edu_reason"])

    print("\n[3] 工作经历门槛")
    work = screening.screen_work(row(工作经历条数=0), rules)
    check("0 段经历 -> 不合格", work["work_result"] == screening.REJECT, work["work_reason"])

    text_row = row(工作经历条数=1, 第1家公司="A公司", 第1家职位="运营", 第1家起止="2024.01-至今",
                   第1家工作内容="负责店铺日常运营")
    check("有经历文本", "运营" in screening.work_text(text_row))

    pending = screening.screen_work(text_row, rules, scorer=None)
    check("未接模型 -> 待评分", pending["work_result"] == "待评分", pending["work_reason"])

    print("\n[4] 整体判定顺序")
    # 知识库 E03：校名未命中指定名单 ≠ 学历造假，只能判"待复核"（未命中与确定不存在必须区分）
    r1 = screening.screen_row(row(学校="某某学院", 学历="本科", 工作经历条数=3), rules)
    check("校名未命中 -> 待复核", r1["最终结论"] == screening.COLLECT_REVIEW
          and r1["经历结论"] == "—", r1["原因"])

    # 学历层次低于大专才由 E01 直接判死
    r0 = screening.screen_row(row(学校="厦门大学", 学历="中专", 工作经历条数=3), rules)
    check("中专直接判死", r0["最终结论"] == screening.REJECT, r0["原因"])

    r2 = screening.screen_row(row(学校="厦门大学", 学历="本科", 工作经历条数=0), rules)
    check("学历通过但无经历 -> 不合格", r2["最终结论"] == screening.REJECT, r2["原因"])

    r3 = screening.screen_row(row(学校="厦门大学", 学历="本科", 工作经历条数=2,
                                 第1家公司="A公司", 第1家职位="运营",
                                 第1家起止="2023.01-至今", 第1家工作内容="负责运营"),
                              rules, scorer=lambda text: 0.9)
    check("有评分器且高于阈值 -> 合适",
          r3["最终结论"] == screening.REVIEW or r3["最终结论"] == screening.PASS,
          f"{r3['最终结论']} 评分={r3.get('模型评分')}")

    print("\n[5] 规则文件")
    path = screening.ensure_rules_file()
    check("规则文件生成", path.exists(), str(path))
    check("默认最低学历=大专", rules["edu"]["min_level"] == "大专")

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
