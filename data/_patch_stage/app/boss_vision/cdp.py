"""通过 Chrome DevTools Protocol 派发鼠标事件：光标完全不移动。

为什么不用 SendInput：SendInput 会真的移动系统光标（用户正在用电脑时会抢鼠标）。
CDP 是在页面内部合成事件，物理光标纹丝不动。

前提：浏览器必须带 --remote-debugging-port=9222 启动。
      现成的 Edge 会话没带这个参数，需要重启一次浏览器（重登 BOSS）。

用法：
    cdp = CDP()
    cdp.click(screen_x, screen_y)      # 屏幕绝对坐标
    cdp.wheel(screen_x, screen_y, -480)  # 负值向下滚
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request

DEFAULT_PORT = int(os.environ.get("CDP_PORT", "9222"))


# ------------------------------------------------------------------ WebSocket

class _WS:
    """极简 WebSocket 客户端（RFC 6455 文本帧，仅客户端侧，够用）。"""

    def __init__(self, url: str, timeout: float = 10.0):
        # ws://127.0.0.1:9222/devtools/page/XXXX
        assert url.startswith("ws://")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败（连接被关闭）")
            data += chunk
        if b"101" not in data.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket 握手失败：{data[:120]!r}")

    def send(self, payload: str) -> None:
        data = payload.encode()
        header = bytearray([0x81])          # FIN + text
        n = len(data)
        if n < 126:
            header.append(0x80 | n)         # 客户端必须掩码
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        def _read(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = self.sock.recv(n - len(buf))
                if not chunk:
                    raise RuntimeError("WebSocket 连接已断开")
                buf += chunk
            return buf

        b1, b2 = _read(2)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", _read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _read(8))[0]
        if b2 & 0x80:                        # 服务端不该掩码，保险起见处理
            mask = _read(4)
        else:
            mask = b""
        data = _read(length)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return data.decode(errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


# ------------------------------------------------------------------ CDP

_WINDOW_TITLE = [""]      # BOSS 窗口标题（含当前活动页标题）
_PINNED_PAGE = [None]      # 固定下来的页面 targetId/ws，所有操作共用同一个页签


class CDP:
    """连到浏览器当前页签，提供 click / wheel / move。"""

    def __init__(self, port: int = DEFAULT_PORT, page_match: str = ""):
        self.port = port
        self._id = 0
        if _PINNED_PAGE[0] is None:
            _PINNED_PAGE[0] = self._page_ws_url(page_match)
        self.ws = _WS(_PINNED_PAGE[0])
        self.view = self._call("Page.getLayoutMetrics") or {}
        # 校验：固定下来的页签确实是 BOSS，否则后续操作可能串到别的页面
        try:
            href = (self._call("Runtime.evaluate",
                               {"expression": "location.href",
                                "returnByValue": True}).get("result") or {}).get("value") or ""
            self.href = str(href)
            if "zhipin.com" not in self.href:
                raise RuntimeError(f"当前 CDP 页签不是 BOSS（{self.href[:60]}），已中止以免误操作")
        except Exception:
            self.href = ""
        self._send("Page.enable")

    # ---------- 发现页签 ----------
    def _page_ws_url(self, page_match: str = "") -> str:
        url = f"http://127.0.0.1:{self.port}/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("浏览器里没有可用页签")
        # 最优先：标题与"屏幕窗口标题"一致的页签（那就是用户正在看的那个）
        wt = _WINDOW_TITLE[0].strip()
        if wt:
            for t in pages:
                pt = (t.get("title") or "").strip()
                if pt and (wt.startswith(pt) or pt.startswith(wt.split(" 和另外")[0].strip())):
                    if "zhipin.com" in (t.get("url") or "") or "BOSS" in pt:
                        return t["webSocketDebuggerUrl"]
        # 其次：BOSS 页签（显式 page_match / zhipin.com / 标题关键字）
        for key in (page_match, "zhipin.com", "BOSS直聘"):
            if not key:
                continue
            for t in pages:
                if key in (t.get("url") or "") or key in (t.get("title") or ""):
                    return t["webSocketDebuggerUrl"]
        return pages[0]["webSocketDebuggerUrl"]

    # ---------- 命令通道 ----------
    def _send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + 10
        while time.time() < deadline:
            raw = self.ws.recv()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败：{msg['error']}")
                return msg.get("result") or {}
        raise TimeoutError(f"{method} 超时")

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self._send(method, params)

    # ---------- 坐标换算：屏幕像素 -> CSS 像素 ----------
    def geometry(self) -> dict:
        """读页面几何信息（CSS 像素），供坐标换算用。"""
        expr = ("JSON.stringify({sx:screenX, sy:screenY, iw:innerWidth, ih:innerHeight,"
                " ow:outerWidth, oh:outerHeight, dpr:devicePixelRatio||1})")
        res = self._call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        try:
            return json.loads(res["result"].get("value") or "{}")
        except Exception:
            return {}

    def set_window_rect(self, rect) -> None:
        """用"截图像素尺寸 / 页面 CSS 尺寸"自动标定缩放比。

        这样换分辨率、改系统缩放、换显示器都不需要手改参数：
            scale = 窗口像素宽 / 页面 CSS 宽（outerWidth）
            内容原点 = 窗口左上 + 边框（由 outer-inner 差值推）
        """
        self.win_rect = tuple(rect) if rect else None
        self.geo = self.geometry()
        geo = self.geo
        self.scale = float(geo.get("dpr") or 1) or 1.0
        if rect and geo.get("ow"):
            try:
                guess = float(rect[2]) / float(geo["ow"])
                # 合理范围才采纳（防止窗口被系统裁剪导致误判）
                if 0.5 <= guess <= 4.0:
                    self.scale = guess
            except Exception:
                pass
        self.calibrated = {
            "win_px": (rect[2], rect[3]) if rect else None,
            "page_css": (geo.get("ow"), geo.get("oh")),
            "inner_css": (geo.get("iw"), geo.get("ih")),
            "scale": round(self.scale, 3),
            "dpr": geo.get("dpr"),
        }

    def _to_css(self, screen_x: int, screen_y: int) -> tuple[float, float]:
        """屏幕坐标 -> 页面 CSS 坐标（分辨率/缩放无关）。

        换算式：css = (screen - 窗口内容原点) / dpr
        内容原点 = 窗口左上角 + 边框偏移（由 outer-inner 差值推）
        """
        geo = getattr(self, "geo", None) or self.geometry()
        dpr = float(getattr(self, "scale", None) or geo.get("dpr") or 1) or 1.0
        rect = getattr(self, "win_rect", None)
        if not rect:
            # 没有窗口信息时退化为"用浏览器自报的屏幕坐标当原点"
            rect = (geo.get("sx", 0), geo.get("sy", 0))
        wx, wy = float(rect[0]), float(rect[1])
        bx = max(0.0, (float(geo.get("ow", 0)) - float(geo.get("iw", 0))) / 2)
        by = max(0.0, float(geo.get("oh", 0)) - float(geo.get("ih", 0))
                 - (float(geo.get("ow", 0)) - float(geo.get("iw", 0))) / 2)
        css_x = (float(screen_x) - wx) / dpr - bx / dpr
        css_y = (float(screen_y) - wy) / dpr - by / dpr
        return css_x, css_y

    # ---------- 对外操作 ----------
    def move(self, x: int, y: int) -> None:
        cx, cy = self._to_css(x, y)
        self._send("Input.dispatchMouseEvent",
                   {"type": "mouseMoved", "x": cx, "y": cy, "button": "none"})

    def click(self, x: int, y: int, button: str = "left") -> None:
        cx, cy = self._to_css(x, y)
        base = {"x": cx, "y": cy, "button": button, "clickCount": 1,
                "buttons": 1 if button == "left" else 2}
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", **base,
                                                "button": "none", "buttons": 0})
        self._send("Input.dispatchMouseEvent", {"type": "mousePressed", **base})
        time.sleep(0.08)
        self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})
        time.sleep(0.08)

    def wheel(self, x: int, y: int, delta_y: int) -> None:
        """delta_y 为负 = 向下滚（和 Windows 滚轮方向一致）。"""
        cx, cy = self._to_css(x, y)
        self._send("Input.dispatchMouseEvent",
                   {"type": "mouseWheel", "x": cx, "y": cy,
                    "deltaX": 0, "deltaY": delta_y})

    def _modal_js(self, action: str, delta_y: int = 0) -> dict:
        """在顶层文档 + 同源 iframe 里找简历弹窗的可滚动容器并操作。

        action: "get"（只读位置）/ "by"（相对滚动）/ "top"（回顶）/ "bottom"（滚到底）
        """
        js = (
            "(() => {"
            " const ACT=ACT_V, DY=DY_V;"
            " const docs=[document];"
            " for (const f of document.querySelectorAll('iframe')) {"
            "   try { if (f.contentDocument) docs.push(f.contentDocument); } catch(e) {} }"
            " const cands=[];"
            " for (const d of docs) {"
            "   for (const e of d.querySelectorAll('div,section,main')) {"
            "     const cs=(d.defaultView||window).getComputedStyle(e);"
            "     if (cs.overflowY!=='auto' && cs.overflowY!=='scroll') continue;"
            "     if (e.scrollHeight <= e.clientHeight + 100) continue;"
            "     if (e.clientHeight < 300) continue;"
            "     cands.push(e);"
            "   }"
            " }"
            " if (!cands.length) return JSON.stringify({ok:false, n:0, moved:0, top:-1});"
            " cands.sort((a,b)=>b.scrollHeight-a.scrollHeight);"
            " const t=cands[0];"
            " const before=t.scrollTop;"
            " if (ACT==='bottom') t.scrollTop=t.scrollHeight;"
            " else if (ACT==='top') t.scrollTop=0;"
            " else if (ACT==='by') t.scrollTop=before+DY;"
            " return JSON.stringify({ok:true, n:cands.length,"
            "   moved:Math.round(t.scrollTop-before), top:Math.round(t.scrollTop),"
            "   sh:t.scrollHeight, ch:t.clientHeight,"
            "   cls:(t.className||'').toString().slice(0,40)});"
            "})()"
        ).replace("ACT_V", json.dumps(action)).replace("DY_V", str(int(delta_y)))
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        try:
            return json.loads(res["result"].get("value") or "{}")
        except Exception:
            return {}

    def scroll_modal(self, delta_y: int) -> int:
        """相对滚动弹窗，返回实际位移（0 表示没动）。"""
        info = self._modal_js("by", delta_y)
        self.last_scroll = info
        return int(info.get("moved") or 0)

    def modal_scroll_top(self) -> int:
        """只读取当前 scrollTop，不改变滚动位置。"""
        info = self._modal_js("get")
        return int(info.get("top", -1))

    def scroll_modal_top(self) -> int:
        """把简历弹窗滚到顶部（含 iframe 内），返回最终 scrollTop。"""
        info = self._modal_js("top")
        self.last_scroll = info
        return int(info.get("top", -1))

    def scroll_modal_bottom(self) -> int:
        info = self._modal_js("bottom")
        self.last_scroll = info
        return int(info.get("moved") or 0)

    def scroll_at(self, x: int, y: int, delta_y: int) -> int:
        """滚"坐标所在的那个滚动容器"，返回滚动后的 scrollTop。

        三级兜底（BOSS 弹窗结构不固定，单靠 elementFromPoint 常找不到）：
          1. elementFromPoint 后往上找可滚动祖先
          2. 遍历所有滚动容器，选"包含该坐标且内容最长"的
          3. 在点位上派发 DOM WheelEvent（触发 React 的滚动处理）
        """
        cx, cy = self._to_css(x, y)
        js = (
            "(() => {"
            " const PX=__CX__, PY=__CY__, DY=__D__;"
            " const scrollIt=(el)=>{ const b=el.scrollTop; el.scrollTop += DY;"
            "   return Math.round(el.scrollTop - b); };"
            " const ok=(el)=>{ if(!el) return false; const cs=getComputedStyle(el);"
            "   return (cs.overflowY==='auto'||cs.overflowY==='scroll')"
            "     && el.scrollHeight > el.clientHeight + 10; };"
            " let doc=document, el=document.elementFromPoint(PX,PY);"
            " if (el && el.tagName==='IFRAME') { try { const d=el.contentDocument;"
            "   doc=d; el=d.elementFromPoint(PX,PY)||d.body; } catch(e){} }"
            " let node=el;"
            " while (node) { if (ok(node)) { const m=scrollIt(node);"
            "   if (m) return JSON.stringify({moved:m, by:'ancestor', top:Math.round(node.scrollTop)}); }"
            "   node=node.parentElement; }"
            " const cands=[...doc.querySelectorAll('*')].filter(e=>{"
            "   if(!ok(e)) return false; const r=e.getBoundingClientRect();"
            "   return r.left<=PX && PX<=r.right && r.top<=PY && PY<=r.bottom; });"
            " for (const t of cands.sort((a,b)=>b.scrollHeight-a.scrollHeight)) {"
            "   const m=scrollIt(t);"
            "   if (m) return JSON.stringify({moved:m, by:'query', top:Math.round(t.scrollTop)}); }"
            " const se=doc.scrollingElement||doc.documentElement;"
            " if (se) { const b=se.scrollTop; se.scrollTop += DY;"
            "   const m=Math.round(se.scrollTop-b);"
            "   if (m) return JSON.stringify({moved:m, by:'document', top:Math.round(se.scrollTop)}); }"
            " const tgt=el||doc.body;"
            " tgt.dispatchEvent(new WheelEvent('wheel',{deltaY:DY,clientX:PX,clientY:PY,bubbles:true,cancelable:true}));"
            " return JSON.stringify({moved:0, by:'wheelEvent', top:0});"
            "})()"
        ).replace("__CX__", str(int(cx))).replace("__CY__", str(int(cy)))
        js = js.replace("__D__", str(int(delta_y)))
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        if res.get("exceptionDetails"):
            raise RuntimeError(str(res["exceptionDetails"].get("text")))
        try:
            info = json.loads(res["result"].get("value") or "{}")
        except Exception:
            return 0
        self.last_scroll = info
        return int(info.get("moved") or 0)

    def scroll_in_container(self, x: float, y: float, delta_y: int) -> int:
        """在 (x,y) 所在的可滚动容器里滚（顶层 + iframe），返回实际位移。

        iframe 内的 elementFromPoint 用的是"帧内坐标"，必须先减去 iframe 偏移。
        """
        js = (
            "(() => {"
            " const PX=__PX__, PY=__PY__, DY=__DY__;"
            " const docs=[[document,0,0]];"
            " for (const f of document.querySelectorAll('iframe')) {"
            "   try { const fr=f.getBoundingClientRect();"
            "     if (f.contentDocument) docs.push([f.contentDocument, fr.left, fr.top]);"
            "   } catch(e) {} }"
            " for (const [d,ox,oy] of docs) {"
            "   const W=(d.defaultView||window);"
            "   let el=null;"
            "   try { el=d.elementFromPoint(PX-ox, PY-oy); } catch(e) {}"
            "   let node=el;"
            "   while (node) {"
            "     const cs=W.getComputedStyle(node);"
            "     if ((cs.overflowY==='auto'||cs.overflowY==='scroll')"
            "         && node.scrollHeight > node.clientHeight + 20) {"
            "       const b=node.scrollTop; node.scrollTop=b+DY;"
            "       const m=Math.round(node.scrollTop-b);"
            "       if (m) return JSON.stringify({moved:m, top:Math.round(node.scrollTop),"
            "         cls:(node.className||'').toString().slice(0,30)});"
            "     }"
            "     node=node.parentElement;"
            "   }"
            " }"
            " return JSON.stringify({moved:0});"
            "})()"
        ).replace("__PX__", str(int(x))).replace("__PY__", str(int(y)))          .replace("__DY__", str(int(delta_y)))
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        try:
            info = json.loads(res["result"].get("value") or "{}")
        except Exception:
            return 0
        self.last_scroll = info
        return int(info.get("moved") or 0)

    def scroll_js(self, delta_y: int) -> int:
        """用 JS 直接滚页面（含 iframe 里的推荐列表），完全不产生鼠标事件。

        BOSS 的推荐列表是虚拟滚动且位于 iframe 中，鼠标滚轮事件不可靠；
        直接设置 scrollingElement.scrollTop 最稳。
        """
        expr = (
            "(() => { const f=document.querySelector('iframe');"
            " const d=(f&&f.contentDocument)||document;"
            " const el=d.scrollingElement||d.documentElement;"
            f" el.scrollTop += ({int(delta_y)});"
            " return Math.round(el.scrollTop); })()"
        )
        res = self._call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return int(res["result"].get("value") or 0)

    def scroll_top(self) -> int:
        expr = (
            "(() => { const f=document.querySelector('iframe');"
            " const d=(f&&f.contentDocument)||document;"
            " const el=d.scrollingElement||d.documentElement;"
            " return Math.round(el.scrollTop||0); })()"
        )
        res = self._call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return int(res["result"].get("value") or 0)

    def modal_open_dom(self) -> bool:
        """DOM 层面判断简历弹窗是否打开（比视觉识别稳）。"""
        js = (
            "(() => {"
            " for (const f of document.querySelectorAll('iframe')) {"
            "   try { const d=f.contentDocument; if(!d) continue;"
            "     const el=d.querySelector('[class*=resumeGreet]');"
            "     if (el) { const r=el.getBoundingClientRect();"
            "       if (r.width>0 && r.height>0) return true; }"
            "   } catch(e) {} }"
            " const el=document.querySelector('[class*=resumeGreet]');"
            " if (el) { const r=el.getBoundingClientRect(); return r.width>0 && r.height>0; }"
            " return false; })()"
        )
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        return bool(res["result"].get("value"))

    def greet_button_text(self) -> str:
        """返回弹窗里「打招呼/继续沟通」按钮当前的文字（找不到返回空串）。"""
        js = (
            "(() => {"
            " const docs=[document];"
            " for (const f of document.querySelectorAll('iframe')) {"
            "   try { if (f.contentDocument) docs.push(f.contentDocument); } catch(e) {} }"
            " for (const d of docs) {"
            "   const W=(d.defaultView||window);"
            "   for (const e of d.querySelectorAll('button,a')) {"
            "     const t=(e.textContent||'').trim();"
            "     if (t!=='打招呼' && t!=='继续沟通') continue;"
            "     const r=e.getBoundingClientRect();"
            "     if (!(r.width>10 && r.height>10)) continue;"
            "     if (!(r.top>=-4 && r.bottom<=W.innerHeight+4)) continue;"
            "     let n=e, inC=false;"
            "     while(n){ if(((n.className||'')+'').indexOf('resumeGreet')>=0){inC=true;break;} n=n.parentElement; }"
            "     if (inC) return t;"
            "   }"
            " }"
            " return ''; })()"
        )
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        return str(res["result"].get("value") or "")

    def screenshot(self):
        """CDP 截图：返回 BGR numpy 数组（窗口被遮挡也能截到）。"""
        import base64
        import io
        import numpy as np
        self._call("Page.enable", {})
        old_timeout = getattr(self.ws, "timeout", None)
        try:
            if hasattr(self.ws, "sock") and self.ws.sock is not None:
                self.ws.sock.settimeout(25.0)
            res = self._call("Page.captureScreenshot", {"format": "png"})
        finally:
            if old_timeout and hasattr(self.ws, "sock") and self.ws.sock is not None:
                self.ws.sock.settimeout(old_timeout)
        data = res.get("data") or (res.get("result") or {}).get("data")
        if not data:
            raise RuntimeError("截图失败")
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
        return np.array(img)[:, :, ::-1].copy()

    def click_css(self, x: float, y: float) -> None:
        """在页面 CSS 坐标处派发真实鼠标事件。"""
        base = {"x": float(x), "y": float(y), "button": "left",
                "clickCount": 1, "buttons": 1}
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(x),
                                                "y": float(y), "button": "none", "buttons": 0})
        time.sleep(0.05)
        self._send("Input.dispatchMouseEvent", {"type": "mousePressed", **base})
        time.sleep(0.06)
        self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})
        time.sleep(0.05)

    def navigate(self, url: str, wait: float = 6.0) -> None:
        self._call("Page.navigate", {"url": url})
        time.sleep(wait)

    def scroll_css(self, x: float, y: float, delta_y: int) -> int:
        """在页面 CSS 坐标处滚轮。"""
        self._send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": float(x),
                                                "y": float(y), "deltaX": 0,
                                                "deltaY": int(delta_y)})
        return 1

    def click_element(self, text: str, container: str = "", exact: bool = True) -> bool:
        """点文字匹配的按钮：候选按「BUTTON 优先、面积最小」排序后派发真实鼠标事件。"""
        js = (
            "(() => {"
            " const T=TXT, C=CONT, EX=EXACT;"
            " const hits=[];"
            " const scan=(d, ox, oy) => {"
            "   const W=(d.defaultView||window);"
            "   for (const e of d.querySelectorAll('a,button,span,div')) {"
            "     const t=(e.textContent||'').trim();"
            "     if (EX ? t!==T : t.indexOf(T)<0) continue;"
            "     const r=e.getBoundingClientRect();"
            "     if (!(r.width>10 && r.height>10)) continue;"
            "     if (!(r.top>=-4 && r.bottom<=W.innerHeight+4 && r.left>=-4 && r.right<=W.innerWidth+4)) continue;"
            "     if (C) { let n=e, ok=false; while(n){ if(((n.className||'')+'').indexOf(C)>=0){ok=true;break;} n=n.parentElement; } if(!ok) continue; }"
            "     hits.push({x:Math.round(r.x+r.width/2+ox), y:Math.round(r.y+r.height/2+oy),"
            "       area:Math.round(r.width*r.height), tag:e.tagName,"
            "       cls:(e.className||'').toString().slice(0,30)});"
            "   }"
            " };"
            " scan(document, 0, 0);"
            " for (const f of document.querySelectorAll('iframe')) {"
            "   try { const fr=f.getBoundingClientRect(); const d=f.contentDocument;"
            "     if (d) scan(d, fr.left, fr.top); } catch(e) {}"
            " }"
            " if (!hits.length) return '';"
            " hits.sort((a,b)=> (a.tag==='BUTTON'?0:1)-(b.tag==='BUTTON'?0:1) || a.area-b.area);"
            " return JSON.stringify(hits[0]); })()"
        ).replace("TXT", json.dumps(text)).replace("CONT", json.dumps(container or "")).replace("EXACT", "true" if exact else "false")
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        info = res["result"].get("value") or ""
        if not info:
            return False
        try:
            box = json.loads(info)
        except Exception:
            return False
        cx, cy = float(box["x"]), float(box["y"])
        base = {"x": cx, "y": cy, "button": "left", "clickCount": 1, "buttons": 1}
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy,
                                                "button": "none", "buttons": 0})
        time.sleep(0.30)
        self._send("Input.dispatchMouseEvent", {"type": "mousePressed", **base})
        time.sleep(0.08)
        self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})
        time.sleep(0.08)
        self.last_click = box
        print(f"     [点击] ({int(cx)},{int(cy)}) {box.get('tag')}.{box.get('cls')} area={box.get('area')}")
        return True

    def click_text(self, text: str, exact: bool = True, container: str = "") -> bool:
        """在可见区域里找文字匹配的元素并 click()。

        要点：页面里同时存在很多「打招呼」按钮（列表里每张卡一个，多数滚出视口），
        必须只点"当前视口内可见"的那个，否则会点到屏幕外的卡片。
        container 用于优先选择位于某个容器（class 关键字）里的按钮，如 resumeGreet。
        """
        js = (
            "(() => {"
            " const T=TXT, EX=EXACT, C=CONT;"
            " const docs=[document];"
            " for (const f of document.querySelectorAll('iframe')) {"
            "   try { if (f.contentDocument) docs.push(f.contentDocument); } catch(e) {} }"
            " const hits=[];"
            " for (const d of docs) {"
            "   const W=(d.defaultView||window);"
            "   for (const e of d.querySelectorAll('a,button,span,div')) {"
            "     const t=(e.textContent||'').trim();"
            "     if (EX ? t!==T : t.indexOf(T)<0) continue;"
            "     const r=e.getBoundingClientRect();"
            "     if (!(r.width>10 && r.height>10)) continue;"
            "     const cs=W.getComputedStyle(e);"
            "     if (cs.visibility==='hidden' || cs.display==='none') continue;"
            "     const visible = r.top>=-4 && r.bottom<=W.innerHeight+4"
            "       && r.left>=-4 && r.right<=W.innerWidth+4;"
            "     if (!visible) continue;"
            "     let inC=false;"
            "     if (C) { let n=e; while(n){ if(((n.className||'')+'').indexOf(C)>=0){inC=true;break;} n=n.parentElement; } }"
            "     hits.push({e, inC, area: r.width*r.height});"
            "   }"
            " }"
            " if (!hits.length) return false;"
            " hits.sort((a,b)=> (b.inC?1:0)-(a.inC?1:0) || a.area-b.area);"
            " hits[0].e.click();"
            " return true; })()"
        ).replace("TXT", json.dumps(text)).replace("EXACT", "true" if exact else "false")
        js = js.replace("CONT", json.dumps(container or ""))
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        return bool(res["result"].get("value"))

    def find_text(self, text: str) -> list[dict]:
        """列出页面上包含 text 的可点元素（诊断用）。"""
        js = (
            "(() => { const T=TXT;"
            " const out=[];"
            " for (const e of document.querySelectorAll('a,button,span,div')) {"
            "   const t=(e.textContent||'').trim();"
            "   if (!t || t.indexOf(T)<0 || t.length>30) continue;"
            "   const r=e.getBoundingClientRect();"
            "   if (!(r.width>10 && r.height>10)) continue;"
            "   out.push({tag:e.tagName, cls:(e.className||'').toString().slice(0,40),"
            "     txt:t.slice(0,20), x:Math.round(r.x), y:Math.round(r.y),"
            "     w:Math.round(r.width), h:Math.round(r.height)});"
            "   if (out.length>=8) break; }"
            " return JSON.stringify(out); })()"
        ).replace("TXT", json.dumps(text))
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        try:
            return json.loads(res["result"].get("value") or "[]")
        except Exception:
            return []

    def press_escape(self) -> None:
        """向页面派发 Esc（关闭弹窗最可靠的方式，不需要窗口焦点）。"""
        for kind in ("rawKeyDown", "keyUp"):
            self._send("Input.dispatchKeyEvent", {
                "type": kind, "key": "Escape", "code": "Escape",
                "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27,
            })

    def close_dialog_by_js(self) -> bool:
        """点弹窗上的关闭按钮（X）。找不到就返回 False。"""
        js = (
            "(() => {"
            " const sels=['[class*=close]','[class*=Close]','.boss-dialog__close',"
            "   '.dialog-close','i[class*=close]'];"
            " for (const s of sels) {"
            "   const el=document.querySelector(s);"
            "   if (el) { const r=el.getBoundingClientRect();"
            "     if (r.width>0 && r.height>0) { el.click(); return true; } } }"
            " return false; })()"
        )
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        return bool(res["result"].get("value"))

    def close(self) -> None:
        self.ws.close()


def list_at_bottom() -> bool:
    """当前页面列表是否已滚到底（到底了再滚也不会有新卡片）。"""
    try:
        c = CDP()
    except Exception:
        return False
    try:
        js = (
            "(() => {"
            " const docs=[document];"
            " for (const f of document.querySelectorAll('iframe')) {"
            "   try { if (f.contentDocument) docs.push(f.contentDocument); } catch(e) {} }"
            " for (const d of docs) {"
            "   const se=d.scrollingElement||d.documentElement;"
            "   if (!se) continue;"
            "   const max=se.scrollHeight-se.clientHeight;"
            "   if (max>1000 && se.scrollTop >= max-8) return true;"
            " }"
            " return false; })()"
        )
        res = c._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        return bool(res["result"].get("value"))
    finally:
        try:
            c.close()
        except Exception:
            pass


def available(port: int = DEFAULT_PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            return True
    except Exception:
        return False
