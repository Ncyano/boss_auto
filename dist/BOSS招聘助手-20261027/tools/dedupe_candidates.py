"""用截图内容指纹（aHash）找出重复候选人并去重。

旧表的截图在 data/hr-shots/，新表的在 data/新人事截图/<招聘人>/。
同一个人可能被不同招聘人录一次、或在同一张表里录了两次（"无名"记录尤其如此）。

用法：
  python tools/dedupe_candidates.py --report     # 只看重复情况
  python tools/dedupe_candidates.py --apply      # 真去重并写回数据集
"""
from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
DATASET = DATA / "运营助理-数据集.csv"
PARSED = DATA / "新人事-解析.csv"
SHOT_DIR_NEW = DATA / "新人事截图"
SHOT_DIR_OLD = DATA / "hr-shots"          # 旧表截图已清理，通常为空

import numpy as np                                  # noqa: E402
from PIL import Image                               # noqa: E402


def ahash(path: Path, size: int = 16) -> int | None:
    """平均哈希：缩到 16x16 灰度，与均值比较，得到 256 位整数。"""
    try:
        img = Image.open(path).convert("L").resize((size, size), Image.BILINEAR)
        arr = np.asarray(img, dtype=np.float32)
        mean = arr.mean()
        bits = (arr > mean).flatten()
        out = 0
        for b in bits:
            out = (out << 1) | int(b)
        return out
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def find_shots(row: dict) -> list[Path]:
    """按行找截图：先找新表目录，再回退旧表目录。"""
    who = (row.get("招聘人") or "").strip()
    seq = (row.get("表内序号") or row.get("序号") or "").strip()
    shots: list[Path] = []

    row_no = (row.get("行号") or row.get("表内序号") or "").strip()
    if not row_no:
        # 老表（未命名表格.xlsx）没有行号、截图也已清理 -> 不参与指纹比对，
        # 否则会拿同名序号去匹配新表的文件，把两个不同的人误判成重复
        return []
    d = SHOT_DIR_NEW / who
    if d.exists():
        # 文件名固定为 <招聘人>-<序号>-r<行号>-<k>.jpg，用行号精确匹配
        for p in d.glob(f"{who}-*-r{row_no}-*"):
            if p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                shots.append(p)

    return sorted(shots)


def signature(row: dict) -> int | None:
    """一条记录的指纹 = 第一张截图的哈希（同一人首屏相同）。"""
    shots = find_shots(row)
    if not shots:
        return None
    return ahash(shots[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真去重并写回")
    ap.add_argument("--threshold", type=int, default=6, help="汉明距离阈值（默认 6）")
    ap.add_argument("--src", default=str(DATASET))
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.src, encoding="utf-8-sig")))
    # 只比对有截图可查的新表记录；旧表没有截图，不能参与
    rows = rows
    fields = list(rows[0].keys())
    print(f"读取 {len(rows)} 条")

    sigs: list[tuple[int, dict]] = []
    no_shot = 0
    for r in rows:
        if (r.get("来源") or "") == "旧表":
            continue
        h = signature(r)
        if h is None:
            no_shot += 1
            continue
        sigs.append((h, r))
    print(f"有截图可算指纹：{len(sigs)} 条；无截图：{no_shot} 条")

    # 聚类
    groups: list[list[dict]] = []
    used = [False] * len(sigs)
    for i, (hi, ri) in enumerate(sigs):
        if used[i]:
            continue
        cluster = [ri]
        used[i] = True
        for j in range(i + 1, len(sigs)):
            if used[j]:
                continue
            if hamming(hi, sigs[j][0]) <= args.threshold:
                cluster.append(sigs[j][1])
                used[j] = True
        if len(cluster) > 1:
            groups.append(cluster)

    dup_rows = sum(len(g) - 1 for g in groups)
    print(f"重复组：{len(groups)} 组，可去掉 {dup_rows} 条")
    for g in groups[:10]:
        names = "/".join((x.get("姓名") or "无名")[:6] for x in g)
        whois = "/".join(x.get("招聘人") or "?" for x in g)
        print(f"  - {names}  ({whois}) 序号 {[x.get('表内序号') or x.get('序号') for x in g]}")

    if not args.apply:
        print("\n（只报告，未修改。加 --apply 执行去重）")
        return 0

    drop_ids = set()
    for g in groups:
        # 保留信息最全的一条：有姓名 > 工作条数多
        def score(x: dict) -> tuple:
            return (1 if (x.get("姓名") or "").strip() else 0,
                    int(x.get("工作条数") or 0))
        keep = max(g, key=score)
        for x in g:
            if x is not keep:
                drop_ids.add(id(x))

    kept = [r for r in rows if id(r) not in drop_ids]
    backup = Path(args.src).with_suffix(".去重前.csv")
    backup.write_text(Path(args.src).read_text(encoding="utf-8-sig"), encoding="utf-8")
    with open(args.src, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)
    print(f"\n去重完成：{len(rows)} -> {len(kept)} 条（去掉 {len(drop_ids)}）")
    print(f"备份：{backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
