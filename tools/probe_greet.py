"""诊断：打开一份在线简历后运行，试三种点击方式，看哪种能让按钮变成「继续沟通」。

用法：手动打开简历弹窗，然后
    .venv\\Scripts\\python.exe tools\\probe_greet.py
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))

import cdp as cdpmod   # noqa: E402
import winenv          # noqa: E402


FIND_JS = """(() => {
  const out = [];
  const docs = [['top', document, {x:0,y:0}]];
  for (const f of document.querySelectorAll('iframe')) {
    try { if (f.contentDocument) {
      const r = f.getBoundingClientRect();
      docs.push(['iframe', f.contentDocument, {x:r.left, y:r.top}]);
    } } catch(e) {}
  }
  for (const [name, d, off] of docs) {
    const W = d.defaultView || window;
    for (const e of d.querySelectorAll('a,button,span,div')) {
      const t = (e.textContent||'').trim();
      if (t !== '打招呼' && t !== '继续沟通') continue;
      const r = e.getBoundingClientRect();
      if (!(r.width>10 && r.height>10)) continue;
      const vis = r.top>=-4 && r.bottom<=W.innerHeight+4 && r.left>=-4 && r.right<=W.innerWidth+4;
      let n = e, inC = false;
      while (n) { if (((n.className||'')+'').indexOf('resumeGreet')>=0) { inC=true; break; } n=n.parentElement; }
      out.push({doc:name, tag:e.tagName, cls:(e.className||'').toString().slice(0,30),
                txt:t, vis, inC, x:Math.round(r.x), y:Math.round(r.y),
                w:Math.round(r.width), h:Math.round(r.height),
                px:Math.round(r.x+r.width/2+off.x), py:Math.round(r.y+r.height/2+off.y)});
    }
  }
  return JSON.stringify(out);
})()"""


def main() -> int:
    if not cdpmod.available():
        print("调试端口没开")
        return 1
    c = cdpmod.CDP()

    def ev(js):
        r = c._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        return r["result"].get("value")

    hits = json.loads(ev(FIND_JS) or "[]")
    print(f"找到 {len(hits)} 个按钮：")
    for h in hits:
        print("  ", json.dumps(h, ensure_ascii=False))

    target = next((h for h in hits if h["inC"] and h["vis"] and h["txt"] == "打招呼"), None)
    if not target:
        print("弹窗里没有可点的「打招呼」按钮")
        return 1

    def state() -> str:
        return c.greet_button_text()

    print("\n当前按钮:", state())
    x, y = target["px"], target["py"]
    print(f"目标页面坐标: ({x}, {y})")

    # ① JS click
    print("\n① 试 JS click ...")
    ev("""(() => { for (const e of document.querySelectorAll('a,button,span,div')) { if ((e.textContent||'').trim()==='打招呼') { const r=e.getBoundingClientRect(); if(r.width>10&&r.top>=0) { e.click(); return 1; } } } 
      for (const f of document.querySelectorAll('iframe')) { try { const d=f.contentDocument; if(!d) continue; for (const e of d.querySelectorAll('a,button')) { const t=(e.textContent||'').trim(); if(t==='打招呼'){ e.click(); return 1; } } } catch(e){} } return 0; })()""")
    time.sleep(1.2)
    print("   -> 按钮:", state())

    # ② CDP 鼠标事件
    if state() == "打招呼":
        print("\n② 试 CDP 鼠标事件 ...")
        c._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0})
        time.sleep(0.15)
        base = {"x": x, "y": y, "button": "left", "clickCount": 1, "buttons": 1}
        c._send("Input.dispatchMouseEvent", {"type": "mousePressed", **base})
        time.sleep(0.05)
        c._send("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})
        time.sleep(1.2)
        print("   -> 按钮:", state())

    # ③ 真实鼠标（会移动光标）
    if state() == "打招呼":
        print("\n③ 试真实鼠标（光标会移动）...")
        geo = c.geometry()
        screen_x = int(x + 7)
        screen_y = int(y + 2)
        print(f"   屏幕坐标 ({screen_x}, {screen_y})")
        u = ctypes.windll.user32
        u.SetCursorPos(screen_x, screen_y)
        time.sleep(0.2)
        u.mouse_event(0x0002, 0, 0, 0, 0)   # left down
        time.sleep(0.05)
        u.mouse_event(0x0004, 0, 0, 0, 0)   # left up
        time.sleep(1.2)
        print("   -> 按钮:", state())

    print("\n最终按钮:", state())
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
