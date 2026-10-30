"""测当前打开的简历：滚到底 → 截图 → 拼接 → 一次识别，对比逐屏识别。"""
from __future__ import annotations

import sys
import time
import pathlib

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import cdp  # noqa: E402
import vlm  # noqa: E402

SEL = ".resume-detail-wrap"
FIND = (
    "(() => {{ const D=[document];"
    " for (const f of document.querySelectorAll('iframe')) {{"
    "   try {{ if (f.contentDocument) D.push(f.contentDocument); }} catch(e) {{}} }}"
    " for (const d of D) {{ const el=d.querySelector('{sel}'); if (el) return el; }}"
    " return null; }})()"
)


def _find_expr(sel: str) -> str:
    return FIND.format(sel=sel)

PROMPT = """你在读 BOSS 直聘候选人简历截图（多屏纵向拼接）。
只读简历内容，忽略顶部导航、右侧账号名、左侧菜单。
姓名只取候选人姓名，不确定留空；不要填招聘者账号名。
只输出 JSON：
{"name":"","age":"","education_mode":"","graduation_year":"","status":"","expect":"",
 "work":[{"company":"","title":"","period":"","descriptions":[]}],
 "education":[{"school":"","major":"","degree":"","period":""}]}"""


def js(cli, expr):
    r = cli._call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return (r.get("result") or {}).get("value")


def main() -> int:
    if not cdp.available():
        print("端口不可用")
        return 1
    cli = cdp.shared()
    cfg = dict(vlm.load_config(), max_image_side=1280, max_tokens=2000)

    info = js(cli, "(() => {"
                   " const D=[document];"
                   " for (const f of document.querySelectorAll('iframe')) {"
                   "   try { if (f.contentDocument) D.push(f.contentDocument); } catch(e) {} }"
                   " for (const d of D) { const el=d.querySelector('.resume-detail-wrap');"
                   "   if (el) return {sh:el.scrollHeight, ch:el.clientHeight}; }"
                   " return null; })()")
    if not info:
        print("没有找到简历弹窗容器")
        return 2
    sh, ch = int(info["sh"]), int(info["ch"])
    max_top = max(0, sh - ch)
    print(f"  弹窗内容 {sh}px / 视口 {ch}px / 可滚 {max_top}px")

    # 位置：顶部 → 中部 → 底部（留 60px 重叠）
    step = ch - 60
    tops = [0]
    while tops[-1] + step < max_top:
        tops.append(tops[-1] + step)
    if tops[-1] != max_top:
        tops.append(max_top)
    print(f"  截图位置: {tops}")

    shots = []
    for t in tops:
        js(cli, "(() => {"
                " const D=[document];"
                " for (const f of document.querySelectorAll('iframe')) {"
                "   try { if (f.contentDocument) D.push(f.contentDocument); } catch(e) {} }"
                " for (const d of D) { const el=d.querySelector('.resume-detail-wrap');"
                f"   if (el) {{ el.scrollTop={t}; return el.scrollTop; }} }}"
                " return -1; })()")
        time.sleep(0.6)
        img = cli.screenshot()
        shots.append(img)
        print(f"    截 {len(shots)} 张  scrollTop={t}  {img.shape[1]}x{img.shape[0]}")

    # A：逐屏识别
    t0 = time.time()
    tok_a = wa = ea = 0
    names_a = []
    for s in shots:
        r = vlm.ask_vision(PROMPT, s, cfg)
        d = r.get("data") or {}
        tok_a += (r.get("usage") or {}).get("total_tokens") or 0
        wa += len(d.get("work") or [])
        ea += len(d.get("education") or [])
        if d.get("name"):
            names_a.append(str(d["name"]))
    dt_a = time.time() - t0

    # B：拼接识别（只去掉重叠带）
    parts = [shots[0]]
    for i in range(1, len(shots)):
        overlap = 60
        parts.append(shots[i][overlap:, :])
    stitched = np.vstack(parts)
    h, w = stitched.shape[:2]
    scale = min(1.0, 1000 / w)
    if scale < 1.0:
        import cv2
        stitched = cv2.resize(stitched, (int(w * scale), int(h * scale)),
                              interpolation=cv2.INTER_AREA)
    print(f"  拼接: {stitched.shape[1]}x{stitched.shape[0]}（{len(shots)} 屏）")

    t1 = time.time()
    rb = vlm.ask_vision(PROMPT, stitched, dict(cfg, max_image_side=2400))
    db = rb.get("data") or {}
    dt_b = time.time() - t1
    tok_b = (rb.get("usage") or {}).get("total_tokens") or 0

    print("\n=== 对比 ===")
    print(f"  逐屏 {len(shots)} 次 : {dt_a:.1f}s  tokens {tok_a}  工作 {wa}  教育 {ea}  姓名 {names_a}")
    print(f"  拼接 1 次     : {dt_b:.1f}s  tokens {tok_b}  工作 {len(db.get('work') or [])}  "
          f"教育 {len(db.get('education') or [])}  姓名 {db.get('name')!r}")
    print(f"  提速 {dt_a/dt_b:.1f}x  token {tok_b-tok_a:+d}")
    print("\n  拼接识别出的公司:")
    for w_ in (db.get("work") or [])[:8]:
        print(f"    · {str(w_.get('company'))[:24]:<24} {str(w_.get('title'))[:18]:<18} {w_.get('period')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
