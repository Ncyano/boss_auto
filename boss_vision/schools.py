"""离线查询《全国普通高等学校名单》。

名单由 tools\\import_school_list.py 从教育部 XLS 转成 data\\schools-normalized.csv。
本模块只做本地查表，不联网。

    python -m boss_vision.schools "泉州纺织服装职业学院"      % 单个查询
    python boss_vision\\schools.py --check-csv runs\\2026-10-21.csv
"""
from __future__ import annotations

import csv
import difflib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LIST_FILES = {
    "general": (DATA / "schools-normalized.csv", DATA / "schools.meta.json"),   # 普通高校
    "adult": (DATA / "schools-adult.csv", DATA / "schools-adult.meta.json"),     # 成人高校
}
ALIAS_JSON = DATA / "school-aliases.json"
LIST_CSV = LIST_FILES["general"][0]        # 兼容旧引用
META_JSON = LIST_FILES["general"][1]

_PUNCT = re.compile(r"[\s\u3000·・.,,。、（）()\[\]【】<>《》\"'“”‘’\-—_/|]+")
_PAREN = re.compile(r"[（(][^）)]*[）)]\s*$")     # 结尾的括号说明，如「（协和学院）」

_cache: dict | None = None


def normalize(name: str) -> str:
    """去掉空格与标点，统一全角括号，便于比对。"""
    text = (name or "").strip()
    text = text.translate(str.maketrans("（）【】", "()[]"))
    return _PUNCT.sub("", text)


def load() -> dict:
    """返回 {规范化校名: 记录} 及元数据。首次调用后缓存。"""
    global _cache
    if _cache is not None:
        return _cache

    records: dict[str, dict] = {}
    metas: dict[str, dict] = {}
    for kind, (csv_path, meta_path) in LIST_FILES.items():
        if not csv_path.exists():
            print(f"[warn] 缺少名单文件 {csv_path}，请先运行 tools\\import_school_list.py")
            continue
        with csv_path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                key = normalize(row.get("学校名称", ""))
                if key and key not in records:      # 普通高校优先
                    row["_list"] = kind
                    records[key] = row
        if meta_path.exists():
            try:
                metas[kind] = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                metas[kind] = {}

    aliases: dict[str, str] = {}
    if ALIAS_JSON.exists():
        try:
            raw = json.loads(ALIAS_JSON.read_text(encoding="utf-8"))
            aliases = {normalize(k): v for k, v in raw.items() if not k.startswith("_")}
        except Exception as exc:
            print(f"[warn] 别名表读取失败：{exc}")

    _cache = {"records": records, "meta": metas.get("general", {}), "metas": metas,
              "aliases": aliases}
    return _cache


def lookup(name: str, fuzzy_min: float = 0.82) -> dict:
    """查一所学校。

    返回 dict：
        listed   是否在名单内（含"去掉括号后缀"后命中）
        matched  命中的规范校名
        province / level  命中记录的省份、办学层次
        method   exact / paren / fuzzy / none
        candidates  模糊匹配的候选（最多 3 个，需人工确认）
    """
    data = load()
    records: dict[str, dict] = data["records"]
    key = normalize(name)
    if not key:
        return {"listed": False, "matched": "", "method": "none", "candidates": []}

    # 名单里有「民办万博科技职业学院」这种带前缀的写法，去掉前缀再试
    for prefix in ("民办", "公办", "省立"):
        if key.startswith(prefix) and len(key) > len(prefix) + 3:
            stripped_key = key[len(prefix):]
            hit2 = records.get(stripped_key)
            if hit2:
                return {"listed": True, "matched": hit2["学校名称"],
                        "province": hit2["所在省份"], "level": hit2["办学层次"],
                        "list": hit2.get("_list", "general"), "method": "paren",
                        "candidates": []}

    hit = records.get(key)
    if hit:
        return {"listed": True, "matched": hit["学校名称"], "province": hit["所在省份"],
                "level": hit["办学层次"], "list": hit.get("_list", "general"),
                "method": "exact", "candidates": []}

    # 别名/曾用名（人工维护，需有来源）
    # 支持两种写法： "旧名": "新名"   或   "旧名": {"target": "新名", "note": "依据"}
    alias = data["aliases"].get(key)
    alias_note = ""
    if isinstance(alias, dict):
        alias_note = str(alias.get("note") or "")
        alias = alias.get("target") or ""
    if alias:
        target = records.get(normalize(str(alias)))
        if target:
            return {"listed": True, "matched": target["学校名称"], "province": target["所在省份"],
                    "level": target["办学层次"], "list": target.get("_list", "general"),
                    "method": "alias", "note": alias_note, "candidates": []}

    # 去掉结尾括号说明再试（名单里是主体名称）
    stripped = normalize(_PAREN.sub("", name))
    if stripped and stripped != key:
        hit = records.get(stripped)
        if hit:
            return {"listed": True, "matched": hit["学校名称"], "province": hit["所在省份"],
                    "level": hit["办学层次"], "list": hit.get("_list", "general"),
                    "method": "paren", "candidates": []}

    # 包含匹配：独立学院/校区/历史写法。名单校名（≥4 字）出现在简历校名里就算命中，
    # 例如「福州大学阳光学院」包含「阳光学院」、「华侨大学厦门工学院」包含「厦门工学院」。
    contained = _contained_match(key, records)
    if contained:
        return contained

    # 模糊匹配：唯一候选时按"写法差异"认定，多个候选则只给候选不判定
    near = difflib.get_close_matches(key, records.keys(), n=5, cutoff=fuzzy_min)
    cand_names = [records[k]["学校名称"] for k in near]

    if len(near) == 1:
        name = near[0]
        ratio = difflib.SequenceMatcher(None, key, name).ratio()
        rec = records[name]
        return {
            "listed": True,
            "matched": rec["学校名称"],
            "province": rec["所在省份"],
            "level": rec["办学层次"],
            "list": rec.get("_list", "general"),
            "method": "fuzzy_unique",
            "note": (f"名单里只有「{rec['学校名称']}」与它相近（相似度 {ratio:.2f}），"
                     f"按写法差异认定，建议核对简历原图"),
            "candidates": cand_names,
        }

    return {
        "listed": False,
        "matched": "",
        "method": "fuzzy" if near else "none",
        "candidates": cand_names,
    }


_ACADEMIC_TAIL = ("大学", "学院", "学校", "职业技术大学", "职业技术学院", "高等专科学校")
# 这些是中小学/幼儿园，不是高校，不能靠"包含大学名"混进名单
_NOT_COLLEGE = ("中学", "小学", "初中", "高中", "幼儿园", "附属中学", "附属小学")


def _contained_match(key: str, records: dict) -> dict | None:
    """名单里的校名是简历校名的子串时，视为同一所学校（独立学院/校区/历史写法）。

    只认长度 ≥4 的名单校名，且要求它的前面是「XX大学/XX学院」这类母体前缀，
    避免「学院」这种泛词误命中。取最长匹配。
    """
    if len(key) < 5:
        return None
    if any(word in key for word in _NOT_COLLEGE):
        return None
    best_score = (-1, -1)          # (出现位置, 长度)：越靠右、越长越优先
    best_hit = None
    for name, rec in records.items():
        name = "".join(ch for ch in name if not ch.isspace())
        if len(name) < 4 or name == key or name not in key:
            continue
        pos = key.index(name)
        prefix = key[:pos]
        if prefix and not prefix.endswith(_ACADEMIC_TAIL):
            continue
        score = (pos, len(name))
        if score > best_score:
            best_score, best_hit = score, rec
    if best_hit:
        return {"listed": True, "matched": best_hit["学校名称"],
                "province": best_hit["所在省份"], "level": best_hit["办学层次"],
                "list": best_hit.get("_list", "general"), "method": "contain",
                "candidates": []}
    return None

def check_names(names: list[str]) -> list[dict]:
    return [{"input": n, **lookup(n)} for n in names]


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="要查的学校名")
    ap.add_argument("--check-csv", default=None, help="对某个 CSV 的「学校」列做批量核对")
    args = ap.parse_args(argv)

    data = load()
    meta = data["meta"]
    if meta:
        print(f"名单：{meta.get('record_count')} 所（截至 {meta.get('list_date')}，"
              f"SHA256 {str(meta.get('source_sha256'))[:12]}…）")

    if args.check_csv:
        path = Path(args.check_csv)
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        col = "学校" if rows and "学校" in rows[0] else "第1个学校"
        print(f"检查 {path.name} 的「{col}」列，共 {len(rows)} 条\n")
        bad = 0
        for row in rows:
            name = (row.get(col) or "").strip()
            if not name:
                continue
            res = lookup(name)
            if res["listed"]:
                mark = "✓"
            elif res["candidates"]:
                mark = "?"
                bad += 1
            else:
                mark = "✗"
                bad += 1
            extra = ""
            if not res["listed"]:
                extra = f"  候选={res['candidates']}" if res["candidates"] else ""
            tag = {"alias": "[别名]", "adult": "[成人]", "general": ""}.get(res.get("list", ""), "")
            if res["method"] == "alias":
                tag = "[别名]"
            elif res.get("list") == "adult":
                tag = "[成人]"
            print(f"  {mark}{tag} {row.get('序号', ''):>3} {row.get('姓名', ''):<8} {name}{extra}")
        print(f"\n不在名单/需确认：{bad} 条")
        return 0

    for name in args.names:
        res = lookup(name)
        if res["listed"]:
            print(f"✓ {name}  ->  {res['matched']}（{res.get('province', '')}，{res.get('level', '')}，{res['method']}）")
        else:
            cand = "、".join(res["candidates"]) or "无"
            print(f"✗ {name}  ->  不在名单；模糊候选：{cand}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
