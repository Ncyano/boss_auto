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
    def _to_css(self, screen_x: int, screen_y: int) -> tuple[float, float]:
        """把屏幕绝对坐标换成页面 CSS 坐标。

        依赖窗口位置 + 页面缩放。取自 Evaluation 里的 window 信息，
        取不到就按 1:1 处理（常见情况下是对的）。
        """
        try:
            probe = ("(() => { const r = window;"
                     " return JSON.stringify({sx: r.screenX, sy: r.screenY,"
                     " ox: r.outerWidth - r.innerWidth, oy: r.outerHeight - r.innerHeight,"
                     " dpr: r.devicePixelRatio}); })()")
            res = self._call("Runtime.evaluate",
                             {"expression": probe, "returnByValue": True})
            info = json.loads(res["result"]["value"])
        except Exception:
            return float(screen_x), float(screen_y)
        # 页面内容左上角 ≈ 窗口左上角 + 边框
        left = info.get("sx", 0) + max(0, (info.get("ox", 0) // 2))
        top = info.get("sy", 0) + (info.get("oy", 0) - max(0, info.get("ox", 0) // 2))
        return float(screen_x - left), float(screen_y - top)

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

    def close(self) -> None:
        self.ws.close()


def available(port: int = DEFAULT_PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            return True
    except Exception:
        return False
