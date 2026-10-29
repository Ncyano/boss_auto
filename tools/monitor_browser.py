"""监控整个浏览器的所有 BOSS 标签页（不限单个页签）。

做法：从 /json 拿到全部 page 目标，为每个页签单独建一条 CDP 连接，
      注入同一套钩子，事件按时间戳合并输出。

用法：
    .venv\\Scripts\\python.exe tools\\monitor_browser.py --seconds 120

录制期间可以随意开新标签、点登录、刷新，都会被抓到。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
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

  // 只包 fetch：记录 URL 与失败状态，不改变任何行为
  const of = window.fetch;
  if (of) window.fetch = function(...args) {
    const u = (typeof args[0] === 'string') ? args[0] : (args[0] && args[0].url) || '';
    return of.apply(this, args).then(r => {
      if (r && r.status >= 400) log(T(), 'fetch-status', r.status, String(u).slice(0, 110));
      return r;
    }, e => { throw e; });
  };

  // 只包 XHR：同样是观察
  const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u, ...rest) {
    this.__u = u; this.__m = m;
    return oo.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.send = function(...a) {
    this.addEventListener('loadend', () => {
      if (this.status >= 400) log(T(), 'xhr-status', this.status, String(this.__u || '').slice(0, 110));
    });
    return os.apply(this, a);
  };

  return 'ok';
})()
"""

LOCK = threading.Lock()
EVENTS: list[dict] = []


def push(tag: str, text: str) -> None:
    with LOCK:
        EVENTS.append({"t": time.time(), "tab": tag, "text": text})


def watch_page(page: dict, deadline: float) -> None:
    tag = (page.get("url") or "")[:60]
    try:
        cdp._PINNED_PAGE[0] = page["webSocketDebuggerUrl"]
        c = cdp.CDP()
    except Exception as exc:
        push(tag, f"[warn] 连接失败: {exc}")
        return
    try:
        c._call("Runtime.enable", {})
        c._call("Log.enable", {})
        c._call("Network.enable", {})
        c._call("Runtime.evaluate", {"expression": HOOK_JS, "returnByValue": True})
        push(tag, "[注入钩子] ok")
        try:
            c.ws.sock.settimeout(0.5)
        except Exception:
            pass
        reqs: dict[str, dict] = {}
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
                txt = " ".join(str(a.get("value", "")) for a in (p.get("args") or []))
                if "[HOOK]" in txt:
                    push(tag, txt.split("[HOOK]", 1)[1].strip())
            elif m == "Runtime.exceptionThrown":
                d = (p.get("exceptionDetails") or {})
                push(tag, f"[异常] {d.get('text')} {str(d.get('exception', {}).get('description'))[:150]}")
            elif m == "Network.requestWillBeSent":
                r = p.get("request") or {}
                reqs[p.get("requestId")] = {"m": r.get("method"), "u": r.get("url"),
                                            "t": p.get("type")}
            elif m == "Network.responseReceived":
                info = reqs.get(p.get("requestId"))
                if info:
                    info["s"] = (p.get("response") or {}).get("status")
                    u = info.get("u") or ""
                    if info.get("s", 200) >= 400 and (info.get("t") in ("XHR", "Fetch") or "/wapi/" in u):
                        push(tag, f"[接口异常] {info['s']} {info.get('m')} {u[:110]}")
    finally:
        try:
            cdp._PINNED_PAGE[0] = None
            c.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=120)
    args = ap.parse_args()

    if not cdp.available():
        print("调试端口没开：先运行 启动调试浏览器.cmd")
        return 1

    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5) as r:
        targets = json.loads(r.read().decode())
    pages = [t for t in targets
             if t.get("type") == "page" and "zhipin.com" in (t.get("url") or "")]
    print(f"  发现 {len(pages)} 个 BOSS 页签：")
    for t in pages:
        print("    " + (t.get("url") or "")[:80])
    if not pages:
        print("  没有 BOSS 页签，请先在浏览器里打开 zhipin.com")
        return 1

    deadline = time.time() + args.seconds
    started: set[str] = set()

    def attach(plist) -> None:
        for pg in plist:
            u = pg.get("webSocketDebuggerUrl") or ""
            if not u or u in started:
                continue
            started.add(u)
            threading.Thread(target=watch_page, args=(pg, deadline), daemon=True).start()

    attach(pages)
    print(f"\n  开始录制 {args.seconds} 秒（会自动纳入新开的标签页）…\n")

    last = 0
    next_scan = time.time() + 2.0
    while time.time() < deadline:
        time.sleep(1.0)
        if time.time() >= next_scan:
            next_scan = time.time() + 2.0
            try:
                with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=3) as r:
                    t2 = json.loads(r.read().decode())
                newp = [t for t in t2 if t.get("type") == "page"
                        and "zhipin.com" in (t.get("url") or "")]
                fresh = [x for x in newp
                         if (x.get("webSocketDebuggerUrl") or "") not in started]
                if fresh:
                    for x in fresh:
                        print(f"   + 新页签: {(x.get('url') or '')[:70]}")
                    attach(fresh)
            except Exception:
                pass
        with LOCK:
            new = EVENTS[last:]
            last = len(EVENTS)
        for e in new:
            print(f"   {time.strftime('%H:%M:%S', time.localtime(e['t']))} "
                  f"[{e['tab'][:28]}] {e['text'][:150]}")

    with LOCK:
        saved = list(EVENTS)
    out = REPO / "boss_vision" / "runs"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"browser-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  共 {len(saved)} 条事件，已保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
