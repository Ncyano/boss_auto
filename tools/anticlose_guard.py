"""拦截「打开开发者工具就关页」的反调试行为（只记录，不破坏业务）。

注入内容：
    · window.close() 变成空操作，并打印调用栈
    · 阻止跳转到 about:blank / 空白页
    · 记录 window.outerWidth-innerWidth 的变化（DevTools 停靠会产生差值）
    · 记录 debugger 相关异常

用法：
    .venv\\Scripts\\python.exe tools/anticlose_guard.py --seconds 120

然后在浏览器里按 F12 打开开发者工具，观察页面是否还会关闭。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))

import cdp  # noqa: E402

GUARD = r"""
(() => {
  if (window.__noClose) return 'already';
  window.__noClose = true;
  const log = (...a) => { try { console.log('[GUARD]', ...a); } catch(e) {} };

  // 1) 禁止自我关闭
  const realClose = window.close.bind(window);
  window.close = function() {
    log('blocked-window.close', new Error().stack.split('\n').slice(1,4).join(' | '));
    return undefined;
  };

  // 2) 禁止跳到空白页
  try {
    const desc = Object.getOwnPropertyDescriptor(Location.prototype, 'href');
    if (desc && desc.set) {
      Object.defineProperty(Location.prototype, 'href', {
        get: desc.get,
        set(v) {
          const s = String(v);
          if (s === 'about:blank' || s === '') { log('blocked-blank', s); return; }
          log('nav', s.slice(0, 90));
          return desc.set.call(this, v);
        },
        configurable: true
      });
    }
  } catch(e) { log('href-err', e.message); }

  // 3) 记录窗口尺寸差（DevTools 停靠会改变 innerWidth）
  let lastDiff = null;
  setInterval(() => {
    const d = (window.outerWidth || 0) - (window.innerWidth || 0);
    if (lastDiff === null) lastDiff = d;
    else if (Math.abs(d - lastDiff) > 80) {
      log('size-shift', lastDiff, '->', d);
      lastDiff = d;
    }
  }, 700);

  // 4) 记录 debugger 触发（异常里的 "debugger" 语句会中断）
  window.addEventListener('error', e => {
    if (e && /debugger/i.test(String(e.message))) log('debugger-hit');
  });

  log('guard-ready');
  return 'ok';
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=120)
    args = ap.parse_args()

    if not cdp.available():
        print("调试端口没开：先运行 启动调试浏览器.cmd")
        return 1

    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5) as r:
        targets = json.loads(r.read().decode())
    pages = [t for t in targets if t.get("type") == "page"
             and "zhipin.com" in (t.get("url") or "")]
    if not pages:
        print("没有 BOSS 页面")
        return 1

    cdp._PINNED_PAGE[0] = pages[0]["webSocketDebuggerUrl"]
    c = cdp.CDP()
    print(f"  页面: {pages[0]['url'][:70]}")

    # 关键：从现在起新建的文档都自动带上守护脚本
    c._call("Page.enable", {})
    try:
        c._call("Page.addScriptToEvaluateOnNewDocument",
                {"source": GUARD, "runImmediately": True})
        print("  守护脚本已注册（新文档自动注入）")
    except Exception as exc:
        print(f"  注册失败：{exc}")
    c._call("Runtime.enable", {})
    c._call("Runtime.evaluate", {"expression": GUARD, "returnByValue": True})
    print(f"  当前页面已注入。现在按 F12 打开开发者工具，观察 {args.seconds} 秒…\n")

    try:
        c.ws.sock.settimeout(0.5)
    except Exception:
        pass
    deadline = time.time() + args.seconds
    n = 0
    while time.time() < deadline:
        try:
            raw = c.ws.recv()
        except Exception:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("method") == "Runtime.consoleAPICalled":
            txt = " ".join(str(a.get("value", "")) for a in
                           (msg.get("params", {}).get("args") or []))
            if "[GUARD]" in txt:
                n += 1
                print("   " + txt.split("[GUARD]", 1)[1].strip()[:150])

    print(f"\n  共 {n} 条守护日志")
    print("  若出现 blocked-window.close，说明确实是反调试在关页面")
    try:
        cdp._PINNED_PAGE[0] = None
        c.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
