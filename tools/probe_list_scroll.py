"""诊断：列表到底靠什么滚动。

用法（保持 BOSS 推荐牛人列表页在前台）：
    .venv\\Scripts\\python.exe tools\\probe_list_scroll.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))

import cdp  # noqa: E402

JS = """(() => {
  const out = [];
  const docs = [['top', document, 0, 0]];
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const r = f.getBoundingClientRect();
      if (f.contentDocument) docs.push(['iframe', f.contentDocument, r.left, r.top]);
    } catch(e) {}
  }
  for (const [name, d, ox, oy] of docs) {
    const se = d.scrollingElement || d.documentElement;
    out.push({doc:name, kind:'scrollingElement', cls:(se.className||'').toString().slice(0,26),
              sh: se.scrollHeight, ch: se.clientHeight, st: Math.round(se.scrollTop)});
    for (const e of d.querySelectorAll('div,section,main,ul')) {
      const cs = (d.defaultView||window).getComputedStyle(e);
      if (!(cs.overflowY==='auto'||cs.overflowY==='scroll'||cs.overflowY==='hidden')) continue;
      if (e.scrollHeight <= e.clientHeight + 20) continue;
      const r = e.getBoundingClientRect();
      out.push({doc:name, kind:'div', cls:(e.className||'').toString().slice(0,26),
                ov: cs.overflowY, sh: e.scrollHeight, ch: e.clientHeight,
                st: Math.round(e.scrollTop),
                x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height)});
      if (out.length > 40) return JSON.stringify(out);
    }
  }
  return JSON.stringify(out);
})()"""


def main() -> int:
    if not cdp.available():
        print("调试端口没开（先 run-edge-debug.cmd）")
        return 1
    c = cdp.CDP()
    info = json.loads(c._call("Runtime.evaluate",
                              {"expression": JS, "returnByValue": True}
                              )["result"].get("value") or "[]")
    print(f"共 {len(info)} 个可滚动候选：\n")
    for it in info:
        print(" ", json.dumps(it, ensure_ascii=False))

    print("\n尝试三种滚动方式：")
    before = c.scroll_top()
    print(f"  documentElement scrollTop 起始 = {before}")
    got = c.scroll_js(360)
    print(f"  scroll_js(360) -> {got}")
    m = c.scroll_in_container(1280, 700, 360)
    print(f"  scroll_in_container(360) -> moved={m} {getattr(c, 'last_scroll', {})}")
    c.scroll_css(1280, 700, 360)
    print(f"  scroll_css(360) -> 已派发滚轮事件")
    print(f"  最终 scrollTop = {c.scroll_top()}")

    print("\n把上面的输出发我，就能确定该滚哪个元素。")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
