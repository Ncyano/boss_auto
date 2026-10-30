"""BOSS 采集 · 本地代理（云端大脑版）

职责：只做「截图 / 滚动 / 点击 / 执行 JS」四件事，把结果发回云端。
识别、判定、知识库全部在云端，因此本机不需要 numpy / opencv / OCR。

依赖：仅标准库 + 本项目 cdp.py（纯标准库实现）。
界面：tkinter 迷你窗口（连接状态 / 当前动作 / 日志），最小化到任务栏即可。

用法：
    python agent.py --cloud ws://<云端>:8000/agent --token <任务token>
    python agent.py --offline          # 不对接云端，只验证 CDP 是否可用
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from urllib.parse import urlparse, urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "boss_vision"))
sys.path.insert(0, HERE)

try:
    import cdp  # noqa: E402
except Exception as exc:  # pragma: no cover
    print(f"无法导入 cdp：{exc}")
    raise


# ---------------------------------------------------------------- WS 客户端
class CloudLink:
    """极简 WebSocket 客户端（复用 cdp._WS），负责收发 JSON 消息。"""

    def __init__(self, url: str, token: str, on_msg, on_state):
        self.url = url
        self.token = token
        self.on_msg = on_msg
        self.on_state = on_state
        self.ws = None
        self.alive = False
        self._lock = threading.Lock()
        self._id = 0

    def _ws_url(self) -> str:
        p = urlparse(self.url)
        scheme = "wss" if p.scheme in ("https", "wss") else "ws"
        return f"{scheme}://{p.netloc}{p.path}?{urlencode({'token': self.token})}"

    def send(self, obj: dict) -> bool:
        with self._lock:
            if self.ws is None:
                return False
            try:
                self.ws.send(json.dumps(obj, ensure_ascii=False))
                return True
            except Exception:
                return False

    def stop(self) -> None:
        self.alive = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    def run_forever(self) -> None:
        backoff = 1.0
        while self.alive:
            try:
                self.on_state("connecting")
                # cdp._WS 需要完整 ws url
                self.ws = cdp._WS(self._ws_url())
                self.on_state("connected")
                backoff = 1.0
                while self.alive:
                    raw = self.ws.recv()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    self.on_msg(msg)
            except Exception as exc:
                self.on_state(f"断开：{exc}")
                self.ws = None
                if not self.alive:
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)


# ---------------------------------------------------------------- 动作执行
class Executor:
    """把云端的命令落到本地浏览器（CDP）。"""

    def __init__(self, log):
        self.log = log
        self._client = None

    def client(self):
        if self._client is None:
            self._client = cdp.shared()
        return self._client

    def reset(self) -> None:
        try:
            cdp.reset_cdp()
        except Exception:
            pass
        self._client = None

    def geometry(self) -> dict:
        """返回分辨率无关的几何信息（云端据此做坐标换算）。

        css_w / css_h  —— 布局视口（= 点击坐标系），与屏幕分辨率、DPI 无关
        dpr / zoom     —— 设备像素比与页面缩放，view_w = css_w * dpr * zoom
        """
        c = self.client()
        res = c._call("Page.getLayoutMetrics") or {}
        lay = res.get("result") or res
        css = lay.get("cssLayoutViewport") or {}
        vis = lay.get("cssVisualViewport") or {}
        g = {}
        try:
            g = c.geometry()
        except Exception:
            pass
        img = None
        try:
            img = c.screenshot()
        except Exception:
            pass
        view = list(img.shape[:2]) if hasattr(img, "shape") else [0, 0]
        css_w = int(css.get("clientWidth") or g.get("iw") or 0)
        css_h = int(css.get("clientHeight") or g.get("ih") or 0)
        return {
            "css_w": css_w,
            "css_h": css_h,
            "dpr": g.get("dpr") or 1,
            "zoom": vis.get("zoom") or 1,
            "scale": vis.get("scale") or 1,
            "view_w": view[1],           # 截图宽（设备像素）
            "view_h": view[0],           # 截图高
            "ratio": round(view[1] / css_w, 4) if css_w else 0,
        }

    def handle(self, msg: dict) -> dict:
        mid = msg.get("id")
        cmd = msg.get("cmd") or ""
        try:
            if cmd == "ping":
                return {"id": mid, "ok": True, "data": "pong"}
            if cmd == "screenshot":
                img = self.client().screenshot()          # BGR ndarray
                import numpy as np
                if isinstance(img, np.ndarray):
                    b64 = base64.b64encode(
                        __import__("cv2").imencode(".jpg", img,
                                                   [int(__import__("cv2").IMWRITE_JPEG_QUALITY), 80])[1]
                    ).decode()
                else:
                    raise RuntimeError("截图返回类型异常")
                return {"id": mid, "ok": True, "data": b64, "format": "jpg",
                        "meta": self.geometry()}
            if cmd == "scroll":
                x, y = int(msg.get("x") or 0), int(msg.get("y") or 0)
                dy = int(msg.get("dy") or 0)
                before = self.client().scroll_top()
                self.client().scroll_js(dy)
                moved = self.client().scroll_top() - before
                if moved == 0:
                    self.client().scroll_css(x, y, dy)
                return {"id": mid, "ok": True, "data": {"moved": moved}}
            if cmd == "click":
                self.client().click_at(int(msg.get("x") or 0), int(msg.get("y") or 0))
                return {"id": mid, "ok": True}
            if cmd == "eval":
                res = self.client()._call("Runtime.evaluate",
                                          {"expression": msg.get("js") or "1",
                                           "returnByValue": True})
                return {"id": mid, "ok": True,
                        "data": (res.get("result") or {}).get("value")}
            if cmd == "url":
                res = self.client()._call("Runtime.evaluate",
                                          {"expression": "location.href",
                                           "returnByValue": True})
                return {"id": mid, "ok": True,
                        "data": (res.get("result") or {}).get("value")}
            if cmd == "geometry":
                return {"id": mid, "ok": True, "data": self.geometry()}
            if cmd == "reset":
                self.reset()
                return {"id": mid, "ok": True}
            return {"id": mid, "ok": False, "error": f"未知命令 {cmd}"}
        except Exception as exc:
            self.reset()
            return {"id": mid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- 界面
class TrayApp:
    def __init__(self, cloud: str, token: str, offline: bool):
        self.offline = offline
        self.log_q: queue.Queue[str] = queue.Queue()
        self.state_q: queue.Queue[str] = queue.Queue()

        self.root = tk.Tk()
        self.root.title("BOSS 采集代理")
        self.root.geometry("460x300")
        self.root.minsize(400, 260)

        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        self.lbl_state = tk.Label(top, text="● 未连接", fg="#b45309",
                                  font=("Microsoft YaHei UI" if os.name == "nt" else "Helvetica", 12, "bold"))
        self.lbl_state.pack(anchor="w")
        self.lbl_cloud = ttk.Label(top, text=f"云端：{cloud or '（离线模式）'}", foreground="#555")
        self.lbl_cloud.pack(anchor="w", pady=(4, 0))
        self.lbl_cdp = ttk.Label(top, text="CDP：检测中…", foreground="#555")
        self.lbl_cdp.pack(anchor="w")

        body = ttk.Frame(self.root, padding=(10, 0))
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, height=8, font=("Consolas", 9) if os.name == "nt"
                            else ("Menlo", 10), bg="#1e2430", fg="#d7e0ea",
                            relief="flat", padx=8, pady=6)
        self.text.pack(fill="both", expand=True)

        self.executor = Executor(self.log)
        self.link = None
        if not offline and cloud:
            self.link = CloudLink(cloud, token, self._on_msg, self._on_state)
            threading.Thread(target=self.link.run_forever, daemon=True).start()

        self.root.after(200, self._pump)
        threading.Thread(target=self._cdp_probe, daemon=True).start()

    # ---- 回调 ----
    def log(self, s: str) -> None:
        self.log_q.put(s)

    def _on_state(self, st: str) -> None:
        self.state_q.put(st)

    def _on_msg(self, msg: dict) -> None:
        """云端指令：在子线程执行，避免阻塞收包。"""
        def run():
            reply = self.executor.handle(msg)
            if self.link:
                self.link.send(reply)
            name = msg.get("cmd")
            ok = reply.get("ok")
            self.log(f"{time.strftime('%H:%M:%S')}  {name} → {'ok' if ok else reply.get('error')}")
        threading.Thread(target=run, daemon=True).start()

    # ---- 定时刷新 ----
    def _pump(self) -> None:
        try:
            while True:
                line = self.log_q.get_nowait()
                self.text.insert("end", line + "\n")
                self.text.see("end")
        except queue.Empty:
            pass
        try:
            while True:
                st = self.state_q.get_nowait()
                if st == "connected":
                    self.lbl_state.configure(text="● 已连接云端", fg="#12915b")
                elif st == "connecting":
                    self.lbl_state.configure(text="● 连接中…", fg="#b45309")
                else:
                    self.lbl_state.configure(text=f"● {st}", fg="#dc2626")
        except queue.Empty:
            pass
        self.root.after(300, self._pump)

    def _cdp_probe(self) -> None:
        for _ in range(30):
            if cdp.available():
                try:
                    g = cdp.shared().geometry()
                    href = cdp.shared()._call("Runtime.evaluate",
                                              {"expression": "location.href",
                                               "returnByValue": True})
                    url = (href.get("result") or {}).get("value") or ""
                    self.root.after(0, lambda: self.lbl_cdp.configure(
                        text=f"CDP：可用 {g.get('iw')}x{g.get('ih')}  {str(url)[:48]}",
                        foreground="#12915b"))
                    self.log(f"CDP 就绪：{url[:60]}")
                except Exception as exc:
                    self.root.after(0, lambda: self.lbl_cdp.configure(
                        text=f"CDP：可用但读取失败 {exc}", foreground="#b45309"))
                return
            time.sleep(1)
        self.root.after(0, lambda: self.lbl_cdp.configure(
            text="CDP：不可用（请先启动带调试端口的浏览器）", foreground="#dc2626"))

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", default="", help="云端地址，如 ws://1.2.3.4:8000/agent")
    ap.add_argument("--token", default="", help="任务 token")
    ap.add_argument("--offline", action="store_true", help="不对接云端，仅验证 CDP")
    args = ap.parse_args()
    TrayApp(args.cloud, args.token, args.offline).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
