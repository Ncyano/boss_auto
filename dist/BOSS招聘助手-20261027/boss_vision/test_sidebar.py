"""侧栏「经历概览」解析与合并的自测：python boss_vision\\test_sidebar.py"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import resume_reader  # noqa: E402

PASS = 0
FAIL = 0
FIX = _HERE / "tests" / "fixtures"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def item(text: str, x: int, y: int, w: int = 120) -> dict:
    return {"text": text, "score": 0.9, "box": [x, y, x + w, y + 18]}


def test_parse_items() -> None:
    print("\n[1] 侧栏条目解析（两行一条：公司+时间 / 职位+时长）")
    items = [
        item("经历概览", 1608, 344),
        item("福州云上源严选科技有..", 1621, 379, 160),
        item("2025.02-2026.09", 1789, 380, 140),
        item("社群运营", 1620, 404),
        item("1年7个月", 1841, 404, 90),
        item("金誉面包店", 1621, 439),
        item("2024.07-2024.08", 1789, 440, 140),
        item("店员/营业员", 1620, 465),
        item("1个月", 1860, 463, 70),
    ]
    got = resume_reader.parse_sidebar_items(items)
    check("解析出 2 段", len(got) == 2, str(len(got)))
    if got:
        check("第一段公司", got[0]["company"].startswith("福州云上源"), got[0]["company"])
        check("第一段职位", got[0]["title"] == "社群运营", got[0]["title"])
        check("第一段时间", got[0]["period"] == "2025.02-2026.09", got[0]["period"])
        check("截断被标出", got[0]["truncated"] is True, str(got[0]["truncated"]))
        check("第二段公司", got[1]["company"] == "金誉面包店", got[1]["company"])
        check("第二段职位", got[1]["title"] == "店员/营业员", got[1]["title"])

    check("没有「经历概览」时返回空", resume_reader.parse_sidebar_items(
        [item("收藏", 1600, 100)]) == [])


def test_merge() -> None:
    print("\n[2] 侧栏合并：补职位 / 补公司名 / 补漏条 / 截断不写")
    work = [
        {"company": "迪盾控股集团有限公司", "title": "", "period": "2026.03-至今",
         "descriptions": [], "tags": []},
        {"company": "", "title": "", "period": "2025.02-2026.09", "descriptions": ["x"], "tags": []},
        {"company": "晋江安雅卫生用品有限公司", "title": "运营", "period": "2024.07-2025.01",
         "descriptions": [], "tags": []},
    ]
    edu: list = []
    sidebar = [
        {"company": "迪盾控股集团有限公司", "title": "国内电商运营", "period": "2026.03 - 至今",
         "truncated": False},
        {"company": "福州云上源严选科技有限公司", "title": "社群运营", "period": "2025.02 - 2026.09",
         "truncated": False},
        {"company": "泉州市展卷文化传播有...", "title": "拼多多运营", "period": "2024.03 - 2024.07",
         "truncated": True},
        {"company": "七色彩虹 (会展)", "title": "新媒体运营", "period": "2025.05 - 2025.06",
         "truncated": False},
        {"company": "福建商学院", "title": "物流管理·本科", "period": "2021 - 2024",
         "truncated": False},
    ]
    res = resume_reader.merge_sidebar(work, edu, sidebar)

    check("补上了缺的职位", work[0]["title"] == "国内电商运营", work[0]["title"])
    check("补上了缺的公司名", work[1]["company"] == "福州云上源严选科技有限公司", work[1]["company"])
    check("已有公司名不被侧栏覆盖", work[2]["company"] == "晋江安雅卫生用品有限公司",
          work[2]["company"])
    check("主区域漏的条目由侧栏补入", res["added_work"] == 1, str(res["added_work"]))
    check("截断的公司名不写入", all("展卷" not in w["company"] for w in work),
          str([w["company"] for w in work]))
    check("截断被计数", res["truncated"] == 1, str(res["truncated"]))
    check("学校类条目进教育", res["added_edu"] == 1 and edu and edu[0]["school"] == "福建商学院",
          str(edu))


def test_ocr_fixture() -> None:
    print("\n[3] 真实截图侧栏（需要 OCR）")
    try:
        import numpy as np
        import vision
        from PIL import Image

        engine = vision.load_ocr()
    except Exception as exc:
        print(f"  [skip] OCR 不可用：{exc}")
        return

    img_path = FIX / "panel-chenlong.png"
    if not img_path.exists():
        print(f"  [skip] 缺少截图 {img_path.name}")
        return

    img = np.asarray(Image.open(img_path).convert("RGB"))[:, :, ::-1].copy()
    entries = resume_reader.read_sidebar(engine, img, vision.Rect(1609, 252, 285, 40),
                                         log=lambda *_: None)
    check("从截图解析出 10 段以上", len(entries) >= 10, str(len(entries)))
    names = [e["company"] for e in entries]
    check("包含 迪盾控股集团有限公司", any("迪盾" in n for n in names), str(names[:3]))
    check("包含 七色彩虹", any("七色彩虹" in n for n in names), str(names))
    title_map = {e["company"]: e["title"] for e in entries}
    check("迪盾对应职位是国内电商运营", title_map.get("迪盾控股集团有限公司") == "国内电商运营",
          str(title_map.get("迪盾控股集团有限公司")))


def main() -> int:
    test_parse_items()
    test_merge()
    test_ocr_fixture()
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
