"""第二轮改动的自测：python boss_vision\\test_round2.py

覆盖：
    · 尺度自适应（不同按钮宽度 -> 几何等比缩放）
    · 跨屏冲突（同时间段不同公司不合并、字段矛盾记冲突）
    · 侧栏与正文不一致（不静默覆盖）
    · 完整导出（work_text 不限段数）
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import resume_reader  # noqa: E402
import screening  # noqa: E402
import stream_parse  # noqa: E402
import vision  # noqa: E402

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


def test_scale() -> None:
    print("\n[1] 尺度自适应")
    base = vision.Rect(1835, 244, 92, 32)
    card1 = vision.card_for(base)
    check("100% 缩放下卡片宽度 1212", card1.w == 1212, str(card1.w))
    check("100% 缩放下点击点 (1275,300)", vision.click_into_resume(base) == (1275, 300),
          str(vision.click_into_resume(base)))

    for factor in (1.25, 1.5, 2.0):
        btn = vision.Rect(1835, 244, int(92 * factor), int(32 * factor))
        card = vision.card_for(btn)
        ratio = card.w / 1212
        check(f"{factor:g}x 缩放时卡片宽度同比", abs(ratio - factor) < 0.03,
              f"{card.w} ({ratio:.2f}x)")

    check("异常尺寸不会算出 0", vision.card_for(vision.Rect(0, 0, 0, 0)).w > 0)

    k = vision.scale_of(184, vision.REF_BUTTON_W)
    band = resume_reader.panel_band.__doc__ is not None
    check("panel_band 使用缩放系数", band and abs(k - 2.0) < 0.01, f"k={k}")


def test_conflicts() -> None:
    print("\n[2] 跨屏冲突：同时间段不同公司")
    s0 = [item("工作经历", 60)] + row("甲公司", "运营", "2024.01 - 2024.06", y=100)
    s1 = row("乙公司", "运营", "2024.01 - 2024.06", y=100)
    p = stream_parse.StreamParser()
    p.feed(s0, screen=0)
    p.feed(s1, screen=1)
    p.close()
    check("两段经历都保留", len(p.work) == 2, str([w["company"] for w in p.work]))
    check("记录冲突", len(p.conflicts) == 1, str(p.conflicts))

    print("\n[3] 字段矛盾：同公司不同职位")
    s2 = [item("工作经历", 60)] + row("甲公司", "运营", "2024.01 - 2024.06", y=100)
    s3 = row("甲公司", "客服", "2024.01 - 2024.06", y=100)
    q = stream_parse.StreamParser()
    q.feed(s2, screen=0)
    q.feed(s3, screen=1)
    q.close()
    check("合并成一条", len(q.work) == 1, str(len(q.work)))
    check("保留先读到的职位", q.work[0]["title"] == "运营", q.work[0]["title"])
    check("记职位冲突", any("职位不一致" in c for c in q.conflicts), str(q.conflicts))


def test_sidebar_conflict() -> None:
    print("\n[4] 侧栏与正文同期不同单位")
    work = [{"company": "甲公司", "title": "运营", "period": "2024.01 - 2024.06",
             "descriptions": ["干运营"], "tags": []}]
    edu: list = []
    sidebar = [{"company": "完全不同的乙公司", "title": "客服", "period": "2024.01 - 2024.06",
                "truncated": False}]
    res = resume_reader.merge_sidebar(work, edu, sidebar)
    check("正文条目不被侧栏覆盖", work[0]["company"] == "甲公司", work[0]["company"])
    check("记为不一致", bool(res.get("conflicts")), str(res.get("conflicts")))
    check("不会悄悄塞一条摘要", all("乙公司" not in (w.get("company") or "") for w in work),
          str([w["company"] for w in work]))


def test_unbounded_export() -> None:
    print("\n[5] 模型输入不限段数")
    row_data = {"序号": 1, "姓名": "多段"}
    for i in range(1, 16):
        row_data[f"第{i}家公司"] = f"公司{i}"
        row_data[f"第{i}家职位"] = f"职位{i}"
        row_data[f"第{i}家起止"] = f"2020.0{i}-2020.1{i}"
        row_data[f"第{i}家工作内容"] = f"职责{i}"
    text = screening.work_text(row_data)
    check("15 段全部进入文本", text.count("职责") == 15, str(text.count("职责")))
    check("顺序按段号", text.index("职责1") < text.index("职责15"))


def main() -> int:
    test_scale()
    test_conflicts()
    test_sidebar_conflict()
    test_unbounded_export()
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
