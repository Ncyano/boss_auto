"""手动诊断：打开一份在线简历后运行，看弹窗能不能被找到并滚动。

用法（保持简历弹窗打开）：
    .venv\\Scripts\\python.exe tools\\probe_modal_scroll.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))

import cdp  # noqa: E402


def main() -> int:
    if not cdp.available():
        print("调试端口没开：先运行 run-edge-debug.cmd，并在那个窗口登录 BOSS")
        return 1

    c = cdp.CDP()

    def ev(expr):
        r = c._call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        if r.get("exceptionDetails"):
            return {"error": str(r["exceptionDetails"].get("text"))}
        raw = r["result"].get("value")
        try:
            return json.loads(raw) if isinstance(raw, str) and raw[:1] in "{[" else raw
        except Exception:
            return raw

    print("① 页面几何")
    print("  ", json.dumps(c.geometry(), ensure_ascii=False))

    print("\n② 顶层文档里的可滚动容器（按内容长度排序）")
    info = ev("""(() => {
      const els = [...document.querySelectorAll('div,section,main,ul')].filter(e => {
        const cs = getComputedStyle(e);
        return (cs.overflowY === 'auto' || cs.overflowY === 'scroll');
      });
      const list = els.map(e => ({
        cls: (e.className || '').toString().slice(0, 46),
        oh: e.scrollHeight, ch: e.clientHeight, st: Math.round(e.scrollTop),
        w: Math.round(e.getBoundingClientRect().width),
        h: Math.round(e.getBoundingClientRect().height)
      })).filter(o => o.oh > o.ch + 20);
      list.sort((a, b) => b.oh - a.oh);
      return JSON.stringify({total: list.length, top: list.slice(0, 8)});
    })()""")
    print("  ", json.dumps(info, ensure_ascii=False, indent=2))

    print("\n③ 尝试滚动最后一个候选容器")
    moved = c.scroll_modal(300)
    print("   moved =", moved, "| 详情:", json.dumps(getattr(c, "last_scroll", {}),
                                                   ensure_ascii=False))
    moved_back = c.scroll_modal(-300)
    print("   回滚 =", moved_back)

    print("\n④ 是否检出弹窗")
    print("  ", ev("JSON.stringify({dialogs: document.querySelectorAll('[class*=dialog],[class*=modal],.boss-dialog').length})"))
    c.close()
    print("\n把 ①②③ 的输出发我，就能定位为什么滚不动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
