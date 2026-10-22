"""学校名单查询的自测：python boss_vision\\test_schools.py"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import schools  # noqa: E402

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


def main() -> int:
    print("[1] 名单加载")
    data = schools.load()
    records = data["records"]
    general = data["metas"].get("general", {})
    adult = data["metas"].get("adult", {})
    check("普通高校名单已导入", general.get("record_count", 0) > 2900, str(general.get("record_count")))
    check("成人高校名单已导入", adult.get("record_count", 0) > 200, str(adult.get("record_count")))
    check("合并索引规模正确", len(records) >= 3100, str(len(records)))
    check("名单版本日期", bool(general.get("list_date")), str(general.get("list_date")))

    print("\n[2] 精确匹配")
    for name, level in [("泉州纺织服装职业学院", "专科"), ("厦门大学", "本科"),
                        ("闽江师范高等专科学校", "专科")]:
        r = schools.lookup(name)
        check(f"{name} 在名单内", r["listed"] and r["method"] == "exact", str(r.get("level")))
        check(f"{name} 层次={level}", r.get("level") == level, str(r.get("level")))

    print("\n[3] 空格/全角括号容错")
    r = schools.lookup(" 厦门 大学 ")
    check("空格被忽略", r["listed"] and r["matched"] == "厦门大学", r["matched"])
    r = schools.lookup("福建师范大学（协和学院）")
    check("括号后缀可退回主体名", r["listed"], f"{r['method']} -> {r['matched']}")

    print("\n[4] 别名表（曾用名/更名）")
    r = schools.lookup("福州职业技术学院")
    check("旧名命中别名并给出新名", r["listed"] and r["method"] == "alias"
          and r["matched"] == "福州职业技术大学", str(r))
    r = schools.lookup("南昌教育学院")
    check("已并入他校的旧名可解析", r["listed"] and r["matched"] == "豫章师范学院", str(r))

    print("\n[5] 不在名单时不乱判")
    r = schools.lookup("某某野鸡职业学院")
    check("未命中", not r["listed"], str(r))
    r = schools.lookup("园林技术")
    check("专业名不会被当成学校", not r["listed"], str(r))


    print("\n[9] 独立学院/历史写法")
    for name, want in [("华侨大学厦门工学院", "厦门工学院"),
                       ("福州大学阳光学院", "阳光学院")]:
        res = schools.lookup(name)
        check(f"{name} 命中名单", res["listed"] and res["matched"] == want,
              f"{res['method']} -> {res['matched']}")
    res = schools.lookup("清华大学附属中学")
    check("中学不算高校", not res["listed"], res["method"])
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
