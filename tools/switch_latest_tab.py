"""一次性脚本：接管 BOSS 推荐页页签并切到「最新」标签。"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, "boss_vision")
import cdp

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
targets = json.loads(op.open("http://127.0.0.1:9222/json", timeout=5).read().decode())
pages = [t for t in targets if t.get("type") == "page"]
print(f"共 {len(pages)} 个页签")

TARGET = "web/chat/recommend"
hit = next((p for p in pages if TARGET in (p.get("url") or "")), None)
boss = next((p for p in pages if "zhipin.com" in (p.get("url") or "")), None)

if hit is None:
    if boss:
        cdp._PINNED_PAGE[0] = boss["webSocketDebuggerUrl"]
        c = cdp.CDP()
        print(f"导航现有 BOSS 页签到推荐页（原 {boss.get('url','')[:50]}）")
        c.navigate("https://www.zhipin.com/web/chat/recommend", wait=6.0)
        c.close()
    else:
        print("没有 zhipin.com 页签，新建页签")
        cdp._PINNED_PAGE[0] = pages[0]["webSocketDebuggerUrl"] if pages else None
        c = cdp.CDP()
        c._call("Target.createTarget", {"url": "https://www.zhipin.com/web/chat/recommend"})
        c.close()
        time.sleep(4.0)
    cdp._PINNED_PAGE[0] = None
    targets = json.loads(op.open("http://127.0.0.1:9222/json", timeout=5).read().decode())
    pages = [t for t in targets if t.get("type") == "page"]
    hit = next((p for p in pages if TARGET in (p.get("url") or "")), None)

if hit is None:
    print("未找到推荐页页签，退出")
    sys.exit(1)

cdp._PINNED_PAGE[0] = hit["webSocketDebuggerUrl"]
c = cdp.CDP()
href = ((c._call("Runtime.evaluate", {"expression": "location.href", "returnByValue": True})
         .get("result") or {}).get("value")) or ""
print("当前页签:", href[:90])

tab_ok = False
for exact in (True, False):
    try:
        tab_ok = bool(c.click_element("最新", exact=exact))
        if tab_ok:
            break
    except Exception as e:
        print("点击异常:", e)
if tab_ok:
    time.sleep(1.5)
print("切到「最新」:", "成功" if tab_ok else "失败")

logged = ((c._call("Runtime.evaluate",
                   {"expression": "document.cookie.indexOf('zp_at')>=0 || !!localStorage.getItem('__zp_stoken__')",
                    "returnByValue": True})
           .get("result") or {}).get("value"))
print("已登录迹象:", "有" if logged else "未检测到")
c.close()
cdp._PINNED_PAGE[0] = None
print("OK - 页签已就绪")
