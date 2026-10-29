"""监控「开第二个标签页导致旧标签页掉线」期间发生了什么。

录三类东西：
    1. 网络请求（含状态码，看是不是心跳接口返回了 401/未登录）
    2. 浏览器控制台输出（脚本主动打点，标 [HOOK]）
    3. JS 异常

并在页面里注入钩子，监听这些"单点登录常见手法"：
    fetch / XHR / WebSocket / BroadcastChannel
    localStorage / sessionStorage 写入
    document.cookie 写入
    visibilitychange / focus / storage 事件

用法：
    .venv\\Scripts\\python.exe tools\\monitor_tabs.py --seconds 40

流程：
    1. 在旧标签页（已登录）里运行本脚本
    2. 然后在新标签页打开 BOSS（或刷新旧标签页）
    3. 等旧标签页出现「账号已经登录过了，请勿重复登录」
    4. 脚本自动停止，输出时间线
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

HOOK_JS = r"""
(() => {
  if (window.__dshHooked) return 'already';
  window.__dshHooked = true;
  const log = (...a) => { try { console.log('[HOOK]', ...a); } catch(e) {} };
  const T = () => new Date().toISOString().slice(11, 23);

  // ---- fetch ----
  const of = window.fetch;
  if (of) window.fetch = function(...args) {
    const u = (typeof args[0] === 'string') ? args[0] : (args[0] && args[0].url) || '';
    log(T(), 'fetch', u.slice(0, 120));
    return of.apply(this, args).then(r => {
      if (r && r.status >= 400) log(T(), 'fetch-status', r.status, u.slice(0, 120));
      return r;
    });
  };

  // ---- XHR ----
  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u, ...rest) {
    this.__u = u; this.__m = m;
    log(T(), 'xhr-open', m, String(u).slice(0, 120));
    return oo.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.send = function(...a) {
    this.addEventListener('loadend', () => {
      if (this.status >= 400) log(T(), 'xhr-status', this.status, String(this.__u || '').slice(0, 120));
    });
    return os.apply(this, a);
  };

  // ---- WebSocket ----
  const OW = window.WebSocket;
  if (OW) {
    window.WebSocket = function(url, p) {
      log(T(), 'ws-open', String(url).slice(0, 120));
      const ws = p ? new OW(url, p) : new OW(url);
      ws.addEventListener('close', e => log(T(), 'ws-close', e.code, String(url).slice(0, 80)));
      return ws;
    };
    window.WebSocket.prototype = OW.prototype;
  }

  // ---- BroadcastChannel ----
  const OBC = window.BroadcastChannel;
  if (OBC) {
    window.BroadcastChannel = function(name) {
      log(T(), 'bc-create', name);
      const ch = new OBC(name);
      const op = ch.postMessage.bind(ch);
      ch.postMessage = function(...a) { log(T(), 'bc-post', name, JSON.stringify(a).slice(0, 200)); return op(...a); };
      ch.addEventListener('message', e => log(T(), 'bc-message', name, JSON.stringify(e.data).slice(0, 200)));
      return ch;
    };
    window.BroadcastChannel.prototype = OBC.prototype;
  }

  // ---- storage ----
  ['localStorage', 'sessionStorage'].forEach(kind => {
    try {
      const st = window[kind];
      const sp = Object.getPrototypeOf(st);
      const osi = sp.setItem, ori = sp.removeItem, ocl = sp.clear;
      sp.setItem = function(k, v) { log(T(), kind + '.setItem', k, String(v).slice(0, 120)); return osi.call(this, k, v); };
      sp.removeItem = function(k) { log(T(), kind + '.removeItem', k); return ori.call(this, k); };
      sp.clear = function() { log(T(), kind + '.clear'); return ocl.call(this); };
    } catch(e) {}
  });

  // ---- cookie ----
  try {
    const d = Document.prototype;
    const desc = Object.getOwnPropertyDescriptor(d, 'cookie');
    if (desc && desc.set) {
      Object.defineProperty(d, 'cookie', {
        get: desc.get,
        set(v) { log(T(), 'cookie.set', String(v).slice(0, 120)); return desc.set.call(this, v); },
        configurable: true
      });
    }
  } catch(e) {}

  // ---- 页面事件 ----
  ['visibilitychange', 'focus', 'blur', 'pagehide', 'pageshow', 'storage', 'online', 'offline']
    .forEach(ev => window.addEventListener(ev, e => {
      log(T(), 'event', ev, document.visibilityState,
          e && e.key ? ('key=' + e.key) : '');
    }));

  // ---- 页面跳转/替换登录 ----
  const orp = history.replaceState, ops = history.pushState;
  history.replaceState = function(...a) { log(T(), 'history.replace', String(a[2] || '').slice(0, 100)); return orp.apply(this, a); };
  history.pushState = function(...a) { log(T(), 'history.push', String(a[2] || '').slice(0, 100)); return ops.apply(this, a); };

  return 'ok';
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=40)
    ap.add_argument("--match", default="",
                    help="只监控 URL 含该串的页签，如 /web/chat/recommend")
    args = ap.parse_args()

    if not cdp.available():
        print("调试端口没开：先运行 启动调试浏览器.cmd")
        return 1

    if args.match:
        cdp._PINNED_PAGE[0] = None          # 解除固定，按 --match 重新选页签
    c = cdp.CDP(args.match) if args.match else cdp.CDP()
    print(f"  旧标签页: {getattr(c, 'href', '')[:80]}")
    print("  注入钩子…")
    print("   ", (c._call("Runtime.evaluate",
                          {"expression": HOOK_JS, "returnByValue": True})
                  .get("result") or {}).get("value"))

    c._send("Runtime.enable")
    c._send("Log.enable")
    c._send("Network.enable")
    try:
        c.ws.sock.settimeout(0.5)
    except Exception:
        pass

    print(f"  开始录制 {args.seconds} 秒 —— 现在去开第二个标签页 / 刷新旧标签页\n")
    lines: list[str] = []
    apis: list[dict] = []
    reqs: dict[str, dict] = {}
    deadline = time.time() + args.seconds

    while time.time() < deadline:
        try:
            raw = c.ws.recv()
        except Exception:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        m = msg.get("method") or ""
        p = msg.get("params") or {}

        if m == "Runtime.consoleAPICalled":
            args_ = p.get("args") or []
            txt = " ".join(str(a.get("value", "")) for a in args_)
            if "[HOOK]" in txt:
                line = txt.replace("[HOOK]", "").strip()
                lines.append(line)
                print("   " + line[:150])
        elif m == "Runtime.exceptionThrown":
            det = (p.get("exceptionDetails") or {}).get("text") or ""
            txt = (p.get("exceptionDetails") or {}).get("exception", {}).get("description", "")
            lines.append(f"[异常] {det} {str(txt)[:160]}")
            print(f"   [异常] {det} {str(txt)[:120]}")
        elif m == "Network.requestWillBeSent":
            r = p.get("request") or {}
            reqs[p.get("requestId")] = {"m": r.get("method"), "u": r.get("url"),
                                         "t": p.get("type")}
        elif m == "Network.responseReceived":
            info = reqs.get(p.get("requestId"))
            if info:
                info["s"] = (p.get("response") or {}).get("status")
                u = info.get("u") or ""
                if (info.get("t") in ("XHR", "Fetch") or "/wapi/" in u) and info.get("s", 200) >= 400:
                    apis.append(info)
                    print(f"   [接口异常] {info['s']} {info['m']} {u[:110]}")

    try:
        c.ws.sock.settimeout(10.0)
    except Exception:
        pass
    c.close()

    print("\n  === 汇总 ===")
    print(f"  钩子日志 {len(lines)} 条，异常接口 {len(apis)} 个")
    if apis:
        print("  出错接口：")
        for a in apis[:15]:
            print(f"    {a.get('s')} {a.get('m')} {a['u'][:110]}")

    out = REPO / "boss_vision" / "runs"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"tabs-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"lines": lines, "bad_apis": apis},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  明细: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
