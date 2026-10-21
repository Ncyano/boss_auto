"""跨屏解析器与采集闸门的自测：python boss_vision\\test_stream.py"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import screening  # noqa: E402
import stream_parse  # noqa: E402

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


def item(text: str, y: int, x: int = 800) -> dict:
    return {"text": text, "score": 0.95, "box": [x, y, x + 300, y + 18]}


def row(*texts: str, y: int) -> list[dict]:
    return [item(t, y, 800 + i * 160) for i, t in enumerate(texts)]


def test_cross_screen() -> None:
    print("\n[1] 跨屏解析：表头被裁 / 公司职位分行 / 章节切换")
    s0 = [item("工作经历", 120), item("2026.03-至今", 160), item("运营平台店铺", 190)]
    s1 = (row("迪盾控股集团有限公司", "国内电商运营", "2026.03 - 至今", y=100)
          + [item("运营平台店铺", 130), item("抖音，多多", 155)]
          + row("醉悠茶叶有限公司", "电商摄影师·运营部", "2025.08 - 2026.03", y=300)
          + [item("业绩：", 330), item("单店铺去除投流成本，盈利10w以上", 360)])
    s2 = ([item("福建满天星有效公司", 100), item("拍摄·新媒体", 130),
           item("2025.02 - 2025.07", 160), item("小红书，视频号以及抖音平台的日更", 190),
           item("教育经历", 400)]
          + row("福建商学院", "物流管理·本科", "2021 - 2024", y=430))

    p = stream_parse.StreamParser()
    for i, s in enumerate([s0, s1, s2]):
        p.feed(s, screen=i)
    p.close()

    work = p.work
    edu = p.education
    check("解析出 3 段工作经历", len(work) == 3, str(len(work)))
    check("被裁表头的那段被合并而不是丢弃",
          any(w["company"] == "迪盾控股集团有限公司" for w in work), str([w["company"] for w in work]))
    check("公司/职位分行时职位没丢",
          any(w["title"] == "拍摄·新媒体" for w in work), str([w["title"] for w in work]))
    check("职责追加到正确条目",
          sum(len(w["descriptions"]) for w in work) >= 4, str([len(w["descriptions"]) for w in work]))
    check("教育经历只解析出 1 段", len(edu) == 1, str(len(edu)))
    if edu:
        check("学校/学历都识别", edu[0]["school"] == "福建商学院" and edu[0]["degree"] == "本科",
              str(edu[0]))
    check("记录来源屏幕", all(w.get("screens") for w in work), str([w.get("screens") for w in work]))


def test_no_false_dedupe() -> None:
    print("\n[2] 不同公司的相同职责文本不会被误删")
    s0 = ([item("工作经历", 60)]
          + row("甲公司", "客服", "2024.01 - 2024.06", y=100)
          + [item("负责客户维护", 130)]
          + row("乙公司", "客服", "2023.01 - 2023.06", y=200)
          + [item("负责客户维护", 230)])
    p = stream_parse.StreamParser()
    p.feed(s0, screen=0)
    p.close()
    check("两段经历都在", len(p.work) == 2, str([w["company"] for w in p.work]))
    check("相同的职责各保留一条",
          all(len(w["descriptions"]) == 1 for w in p.work),
          str([w["descriptions"] for w in p.work]))


def test_collection_gate() -> None:
    print("\n[3] 采集质量闸门")
    rules = screening.load_rules()

    ok_row = {"序号": 1, "姓名": "正常", "学校": "厦门大学", "学历": "本科", "工作经历条数": 2,
              "已打开": "是", "采集完成": "是", "读到章节": "work/edu", "需复核": "否",
              "复核原因": ""}
    gate = screening.screen_collection(ok_row, rules)
    check("数据完整时不拦", gate["collect_ok"], gate["collect_reason"])

    not_opened = dict(ok_row, **{"已打开": "否"})
    check("简历没打开 -> 拦", not screening.screen_collection(not_opened, rules)["collect_ok"])

    no_work_section = dict(ok_row, **{"工作经历条数": 0, "读到章节": "edu"})
    gate2 = screening.screen_collection(no_work_section, rules)
    check("工作经历章节没读到 -> 拦", not gate2["collect_ok"], gate2["collect_reason"])

    empty_work = dict(ok_row, **{"工作经历条数": 0, "读到章节": "work/edu",
                                 "需复核": "是", "复核原因": "工作经历 0 条"})
    gate3 = screening.screen_collection(empty_work, rules)
    check("章节读到但确实 0 条 -> 不拦（交给业务规则）", gate3["collect_ok"], gate3["collect_reason"])

    no_degree = dict(ok_row, **{"学历": ""})
    check("第一段教育缺学历 -> 拦", not screening.screen_collection(no_degree, rules)["collect_ok"])

    truncated = dict(ok_row, **{"需复核": "是", "复核原因": "1 段经历缺公司名"})
    gate4 = screening.screen_collection(truncated, rules)
    check("缺公司名 -> 拦", not gate4["collect_ok"], gate4["collect_reason"])

    out = screening.screen_row(truncated, rules)
    check("整体结论为「采集待复核」", out["最终结论"] == screening.COLLECT_REVIEW, out["最终结论"])
    check("采集待复核时不做学历判断", out["教育结论"] == "—", str(out.get("教育结论")))
    check("采集待复核时不进模型评分", out.get("经历结论") == "—", str(out.get("经历结论")))


def test_completion_states() -> None:
    print("\n[4] 完成状态三分类")
    from resume_reader import ReadResult

    done = ReadResult(reached_education=True, complete=True, stopped_reason="读取完成（已读到教育经历）")
    check("读取完成", done.complete and done.reached_education)

    bottom = ReadResult(reached_education=False, complete=False,
                        stopped_reason="页面未移动（判断为到底，未读到教育经历）")
    check("页面未移动是独立状态", "页面未移动" in bottom.stopped_reason and not bottom.complete,
          bottom.stopped_reason)

    capped = ReadResult(reached_education=False, complete=False,
                        stopped_reason="达到滚动上限 12 屏（未读到教育经历）")
    check("达到上限 -> 未完成", "上限" in capped.stopped_reason and not capped.complete)


def main() -> int:
    test_cross_screen()
    test_no_false_dedupe()
    test_collection_gate()
    test_completion_states()
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
