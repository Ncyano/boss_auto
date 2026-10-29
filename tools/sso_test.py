"""单浏览器双标签「不顶号」实验（仅本地注入，不涉及凭证抓取）。

目标：观察在固定设备指纹 + 拦截标签页间广播/存储事件后，
      旧标签页是否还会被新标签页踢下线。

做法：
    1. 找到旧标签页（已登录，推荐牛人页）与新标签页
    2. 给新标签页注入守卫脚本（Page.addScriptToEvaluateOnNewDocument）
       · 固定 warlockjssdkcross（设备指纹）
       · 拦截 BroadcastChannel 的 postMessage / message（阻断顶号广播）
       · 冻结 localStorage 里登录态相关 key 的删除
    3. 刷新新标签页使其生效
    4. 观察 60 秒内旧标签页是否跳转登录页 / 出现 401

用法：
    .venv\\Scripts\\python.exe tools/sso_test.py --old "/web/chat" --new "/"
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

GUARD_JS = r"""
(() => {
  if (window.__ssoGuard) return 'already';
  window.__ssoGuard = true;
  const log = (...a) => { try { console.log('[GUARD]', ...a); } catch(e) {} };

  // 1) 固定设备指纹
  const KEY = 'warlockjssdkcross';
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const fixed = raw;
      const sp = Storage.prototype.setItem;
      Storage.prototype.setItem = function(k, v) {
        if (String(k).indexOf(KEY) >= 0) { log('freeze-fp'); return sp.call(this, k, fixed); }
        return sp.call(this, k, v);
      };
      Object.defineProperty(window, '__ssoFp', { value: fixed, configurable: true });
      log('fp-pinned', String(fixed).slice(0, 60));
    }
  } catch(e) { log('fp-err', e.message); }

  // 2) 阻断顶号广播（只拦"通知类"，不拦业务数据）
  const OBC = window.BroadcastChannel;
  if (OBC) {
    window.BroadcastChannel = function(name) {
      const ch = new OBC(name);
      const op = ch.postMessage.bind(ch);
      ch.postMessage = function(...a) {
        const s = JSON.stringify(a);
        if (/logout|login|kick|offline|invalid|expire/i.test(s)) {
          log('bc-blocked-post', name, s.slice(0, 120));
          return;
        }
        return op(...a);
      };
      ch.addEventListener('message', e => {
        const s = JSON.stringify(e.data);
        if (/logout|login|kick|offline|invalid|expire/i.test(s)) {
          log('bc-blocked-recv', name, s.slice(0, 120));
          e.stopImmediatePropagation();
        }
      }, true);
      return ch;
    };
    window.BroadcastChannel.prototype = OBC.prototype;
  }

  // 3) 冻结登录态 key 的删除（不含自动登出接口）
  const PROTECT = ['__zp_stoken__', 'zp_token', 'BOSS_TOKEN', 'uid', 'userInfo'];
  const rd = Storage.prototype.removeItem;
  Storage.prototype.removeItem = function(k) {
    if (PROTECT.some(p => String(k).indexOf(p) >= 0)) {
      log('blocked-remove', k);
      return;
    }
    return rd.call(this, k);
  };

  // 4) 记录页面跳转到登录页
  const orp = history.replaceState;
  history.replaceState = function(...a) {
    const u = String(a[2] || '');
    if (u.indexOf('user') >= 0 || u.indexOf('login') >= 0) log('jump-login', u.slice(0, 100));
    return orp.apply(this, a);
  };
  log('guard-ready');
  return 'ok';
})()
"""


def pick(match: str):
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5) as r:
        targets = json.loads(r.read().decode())
    pages = [t for t in targets if t.get("type") == "page"
             and "zhipin.com" in (t.get("url") or "")]
    for p in pages:
        if match and match in (p.get("url") or ""):
            return p
    return None


def connect(page: dict) -> cdp.CDP:
    cdp._PINNED_PAGE[0] = page["webSocketDebuggerUrl"]
    return cdp.CDP()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="/web/chat", help="旧标签页 URL 关键字")
    ap.add_argument("--new", default="/", help="新标签页 URL 关键字")
    ap.add_argument("--seconds", type=int, default=60)
    args = ap.parse_args()

    if not cdp.available():
        print("调试端口没开")
        return 1

    old_p = pick(args.old)
    if not old_p:
        print(f"找不到旧标签页（含 {args.old}）")
        return 1
    # 新标签页 = 除旧页签之外的那个 BOSS 页签（"--" 通配）
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5) as r:
        targets = json.loads(r.read().decode())
    others = [t for t in targets if t.get("type") == "page"
              and "zhipin.com" in (t.get("url") or "")
              and t.get("id") != old_p.get("id")]
    new_p = others[0] if others else None
    print(f"  旧标签页: {old_p['url'][:70]}")
    print(f"  新标签页: {new_p['url'][:70] if new_p else '(没有，请先开第二个标签页)'}")
    if not new_p:
        return 1

    # 旧标签页：只监听，不注入
    old_c = connect(old_p)
    old_c._call("Runtime.enable", {})
    old_c._call("Log.enable", {})
    old_c._call("Network.enable", {})

    # 新标签页：注入守卫
    new_c = connect(new_p)
    new_c._call("Page.enable", {})
    print("  注入守卫到新标签页…")
    try:
        r = new_c._call("Page.addScriptToEvaluateOnNewDocument",
                        {"source": GUARD_JS, "runImmediately": True})
        print("   ", json.dumps(r.get("result") or {}, ensure_ascii=False)[:120])
    except Exception as exc:
        print("   addScript 失败（继续用立即执行）：", exc)
    new_c._call("Runtime.evaluate", {"expression": GUARD_JS, "returnByValue": True})
    new_c._call("Page.reload", {"ignoreCache": False})
    print("  新标签页已刷新，守卫生效。观察旧标签页 "
          f"{args.seconds} 秒…\n")

    try:
        old_c.ws.sock.settimeout(0.5)
    except Exception:
        pass
    deadline = time.time() + args.seconds
    events: list[str] = []
    while time.time() < deadline:
        try:
            raw = old_c.ws.recv()
        except Exception:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        m, p = msg.get("method") or "", msg.get("params") or {}
        if m == "Runtime.consoleAPICalled":
            txt = " ".join(str(a.get("value", "")) for a in (p.get("args") or []))
            if "[GUARD]" in txt:
                events.append(txt)
                print("   新tab " + txt[:140])
        elif m == "Network.responseReceived":
            resp = p.get("response") or {}
            if resp.get("status") in (401, 403):
                line = f"旧tab {resp.get('status')} {resp.get('url','')[:100]}"
                events.append(line)
                print("   " + line)

    print(f"\n  旧标签页 {'出现' if events else '没有出现'}掉线迹象")
    print("  （若界面弹「账号已登录过」但你看到这里没有 401，说明是服务端在下次请求时才作废）")
    for c in (old_c, new_c):
        try:
            cdp._PINNED_PAGE[0] = None
            c.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
