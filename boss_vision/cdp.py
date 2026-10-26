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

class CDP:
    """连到浏览器当前页签，提供 click / wheel / move。"""

    def __init__(self, port: int = DEFAULT_PORT, page_match: str = ""):
        self.port = port
        self._id = 0
        self.ws = _WS(self._page_ws_url(page_match))
        self.view = self._call("Page.getLayoutMetrics") or {}
        self._send("Page.enable")

    # ---------- 发现页签 ----------
    def _page_ws_url(self, page_match: str) -> str:
        url = f"http://127.0.0.1:{self.port}/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("浏览器里没有可用页签")
        if page_match:
            for t in pages:
                if page_match in (t.get("url") or "") or page_match in (t.get("title") or ""):
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
        self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})

    def wheel(self, x: int, y: int, delta_y: int) -> None:
        """delta_y 为负 = 向下滚（和 Windows 滚轮方向一致）。"""
        cx, cy = self._to_css(x, y)
        self._send("Input.dispatchMouseEvent",
                   {"type": "mouseWheel", "x": cx, "y": cy,
                    "deltaX": 0, "deltaY": delta_y})

    def scroll_modal_top(self) -> int:
        """把简历弹窗滚到顶部，返回最终 scrollTop（应接近 0）。"""
        js = (
            "(() => {"
            " const els=[...document.querySelectorAll('div,section,main')].filter(e=>{"
            "   const cs=getComputedStyle(e);"
            "   return (cs.overflowY==='auto'||cs.overflowY==='scroll')"
            "     && e.scrollHeight > e.clientHeight + 100 && e.clientHeight > 300; });"
            " if(!els.length) return -1;"
            " els.sort((a,b)=>b.scrollHeight-a.scrollHeight);"
            " const t=els[0]; t.scrollTop = 0;"
            " return Math.round(t.scrollTop);"
            "})()"
        )
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        val = res["result"].get("value")
        return -1 if val is None else int(val)

    def modal_scroll_top(self) -> int:
        js = (
            "(() => {"
            " const els=[...document.querySelectorAll('div,section,main')].filter(e=>{"
            "   const cs=getComputedStyle(e);"
            "   return (cs.overflowY==='auto'||cs.overflowY==='scroll')"
            "     && e.scrollHeight > e.clientHeight + 100 && e.clientHeight > 300; });"
            " if(!els.length) return -1;"
            " els.sort((a,b)=>b.scrollHeight-a.scrollHeight);"
            " return Math.round(els[0].scrollTop);"
            "})()"
        )
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        val = res["result"].get("value")
        return -1 if val is None else int(val)

    def scroll_modal(self, delta_y: int) -> int:
        """滚简历弹窗：不依赖坐标，直接找顶层文档里"内容最长的可滚动容器"。

        换机/换分辨率后坐标可能偏，这种找法不受影响。
        """
        js = (
            "(() => {"
            " const els=[...document.querySelectorAll('div,section,main')].filter(e=>{"
            "   const cs=getComputedStyle(e);"
            "   return (cs.overflowY==='auto'||cs.overflowY==='scroll')"
            "     && e.scrollHeight > e.clientHeight + 100"
            "     && e.clientHeight > 300"
            "     && e.getBoundingClientRect().width > 400; });"
            " if(!els.length) return JSON.stringify({moved:0,n:0});"
            " els.sort((a,b)=>b.scrollHeight-a.scrollHeight);"
            " const t=els[0], b=t.scrollTop;"
            " t.scrollTop += DY;"
            " return JSON.stringify({moved:Math.round(t.scrollTop-b), n:els.length,"
            "   top:Math.round(t.scrollTop), sh:t.scrollHeight, ch:t.clientHeight,"
            "   cls:(t.className||'').toString().slice(0,50)});"
            "})()"
        ).replace("DY", str(int(delta_y)))
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        try:
            info = json.loads(res["result"].get("value") or "{}")
        except Exception:
            return 0
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

    def click_text(self, text: str, exact: bool = True) -> bool:
        """在页面里找文字等于/包含 text 的可点击元素并 click()。"""
        js = (
            "(() => {"
            " const T=TXT, EX=EXACT;"
            " const els=[...document.querySelectorAll('a,button,span,div')];"
            " const hit=els.find(e=>{"
            "   const t=(e.textContent||'').trim();"
            "   if (EX ? t!==T : t.indexOf(T)<0) return false;"
            "   const r=e.getBoundingClientRect();"
            "   if (!(r.width>10 && r.height>10)) return false;"
            "   const cs=getComputedStyle(e);"
            "   return cs.visibility!=='hidden' && cs.display!=='none';"
            " });"
            " if(!hit) return false;"
            " hit.click(); return true;"
            "})()"
        ).replace("TXT", json.dumps(text)).replace("EXACT", "true" if exact else "false")
        res = self._call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        return bool(res["result"].get("value"))

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


def available(port: int = DEFAULT_PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            return True
    except Exception:
        return False
