"""A/B：3 屏分别识别 vs 拼接长图一次识别。

用法：
    python tools/bench_stitch.py --screens 3
"""
from __future__ import annotations

import argparse
import sys
import time
import pathlib

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import cdp  # noqa: E402
import vlm  # noqa: E402

PROMPT = """你在读 BOSS 直聘候选人简历的截图（可能由多屏纵向拼接而成）。
只读简历内容，忽略顶部导航、右侧账号名、左侧菜单；姓名只取候选人姓名，不确定留空。
只输出 JSON：
{"name":"","age":"","education_mode":"","graduation_year":"","status":"","expect":"",
 "work":[{"company":"","title":"","period":"","descriptions":[]}],
 "education":[{"school":"","major":"","degree":"","period":""}]}"""


SEL = ".resume-detail-wrap"


def _sel_top(cli) -> int:
    r = cli._call("Runtime.evaluate", {"expression":
        f"(document.querySelector('{SEL}')||{{}}).scrollTop||0",
        "returnByValue": True})
    return int((r.get("result") or {}).get("value") or 0)


def scroll_modal(cli, dy: int) -> int:
    """优先直接滚 .resume-detail-wrap（实测它就是简历弹窗的滚动容器）。"""
    try:
        before = _sel_top(cli)
        cli._call("Runtime.evaluate", {"expression":
            f"(() => {{const el=document.querySelector('{SEL}');"
            f"if(!el) return 0; el.scrollTop += {int(dy)}; return el.scrollTop;}})()",
            "returnByValue": True})
        time.sleep(0.4)
        moved = _sel_top(cli) - before
        if moved:
            return moved
    except Exception:
        pass
    # ① JS 直接设 scrollTop
    try:
        before = cli.modal_scroll_top()
        cli.scroll_modal(dy)
        time.sleep(0.4)
        moved = cli.modal_scroll_top() - before
        if moved:
            return moved
    except Exception:
        pass
    # ② 在弹窗中心派发滚轮
    try:
        g = cli.geometry()
        before = cli.modal_scroll_top()
        cli.wheel(int(g["iw"] * 0.5), int(g["ih"] * 0.5), -dy)
        time.sleep(0.5)
        moved = cli.modal_scroll_top() - before
        if moved:
            return moved
    except Exception:
        pass
    # ③ 直接滚所有可滚动容器
    try:
        before = cli.modal_scroll_top()
        cli._call("Runtime.evaluate", {"expression": (
            "(() => {let n=0;"
            "for (const d of [document, ...[...document.querySelectorAll('iframe')]"
            ".map(f=>{try{return f.contentDocument}catch(e){return null}}).filter(Boolean)]) {"
            " for (const el of d.querySelectorAll('*')) {"
            "   if (el.scrollHeight - el.clientHeight > 200) {"
            "     el.scrollTop += " + str(int(dy)) + "; n++; } } }"
            "return n;})()"), "returnByValue": True})
        time.sleep(0.5)
        return cli.modal_scroll_top() - before
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screens", type=int, default=3)
    ap.add_argument("--step", type=int, default=700)
    args = ap.parse_args()

    if not cdp.available():
        print("调试端口不可用")
        return 1

    cli = cdp.shared()
    cfg = dict(vlm.load_config(), max_image_side=1280, max_tokens=2000)

    # 回到顶部再截
    try:
        cli.scroll_modal_top()
        time.sleep(0.6)
    except Exception:
        pass

    shots = []
    for i in range(args.screens):
        shots.append(cli.screenshot())
        print(f"  截图 {i+1}/{args.screens}  {shots[-1].shape[1]}x{shots[-1].shape[0]}")
        if i < args.screens - 1:
            moved = scroll_modal(cli, args.step)
            print(f"    滚动 {args.step} → 实际 {moved}")
            time.sleep(0.8)

    # ---- A：逐屏识别 ----
    t0 = time.time()
    tok_a, works_a, edus_a, names_a = 0, 0, 0, []
    for s in shots:
        r = vlm.ask_vision(PROMPT, s, cfg)
        d = r.get("data") or {}
        tok_a += (r.get("usage") or {}).get("total_tokens") or 0
        works_a += len(d.get("work") or [])
        edus_a += len(d.get("education") or [])
        if d.get("name"):
            names_a.append(str(d["name"]))
    dt_a = time.time() - t0

    # ---- B：拼接长图一次识别 ----
    stitched = np.vstack(shots)
    h, w = stitched.shape[:2]
    scale = min(1.0, 1000 / w)
    if scale < 1.0:
        import cv2
        stitched = cv2.resize(stitched, (int(w * scale), int(h * scale)),
                              interpolation=cv2.INTER_AREA)
    print(f"  拼接尺寸: {stitched.shape[1]}x{stitched.shape[0]}")

    t1 = time.time()
    rb = vlm.ask_vision(PROMPT, stitched, dict(cfg, max_image_side=2000))
    db = rb.get("data") or {}
    tok_b = (rb.get("usage") or {}).get("total_tokens") or 0
    dt_b = time.time() - t1

    print("\n=== 结果对比 ===")
    print(f"  逐屏 {args.screens} 次 : 耗时 {dt_a:.1f}s   tokens {tok_a}   "
          f"工作 {works_a}  教育 {edus_a}  姓名 {names_a[:3]}")
    print(f"  拼接 1 次      : 耗时 {dt_b:.1f}s   tokens {tok_b}   "
          f"工作 {len(db.get('work') or [])}  教育 {len(db.get('education') or [])}  "
          f"姓名 {db.get('name')!r}")
    print(f"\n  提速: {dt_a / dt_b:.1f}x    token 变化: {tok_b - tok_a:+d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
