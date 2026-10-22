"""视觉模型读取链路的集成测试（用本地假端点，不需要真 key、不花钱）。

起一个本地 HTTP 服务模拟 OpenAI 兼容的 /chat/completions，返回预置 JSON，
再让 read_resume_vlm 走一遍：截图 -> 调用 -> 归一 -> 跨屏合并 -> 完成判定。
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import resume_reader  # noqa: E402
import vision  # noqa: E402
import vlm  # noqa: E402
import winenv  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


SCREENS = [
    {"name": "测试候选人", "sections": [
        {"section": "work", "company": "福州甲科技有限公司", "title": "运营",
         "period": "2025.02-至今", "responsibilities": ["负责店铺日常运营"], "tags": ["电商运营"]},
    ]},
    {"sections": [
        {"section": "work", "company": "", "title": "运营", "period": "2025.02-至今",
         "responsibilities": ["负责店铺日常运营", "对接供应商"], "tags": []},
        {"section": "work", "company": "福建乙贸易有限公司", "title": "客服",
         "period": "2023.06-2025.01", "responsibilities": ["接待客户"], "tags": []},
    ]},
    {"sections": [
        {"section": "education", "school": "厦门大学", "major": "广告学", "degree": "本科",
         "period": "2019-2023"},
    ]},
]


class Handler(BaseHTTPRequestHandler):
    call = 0
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        Handler.received.append({
            "model": body.get("model"),
            "has_image": any(isinstance(c, dict) and c.get("type") == "image_url"
                             for c in (body.get("messages") or [{}])[0].get("content", [])),
            "auth": bool(self.headers.get("Authorization")),
        })
        # 姓名专用请求（max_tokens 很小）返回纯姓名，模拟真实端点的行为
        if int(body.get("max_tokens") or 0) <= 60:
            content = "测试候选人"          # 姓名请求：不推进屏序
        else:
            content = json.dumps(SCREENS[min(Handler.call, len(SCREENS) - 1)], ensure_ascii=False)
            Handler.call += 1
        resp = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 180},
        }
        data = json.dumps(resp, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"假端点已启动: http://127.0.0.1:{port}\n")

    cfg = vlm.load_config()
    cfg.update({"base_url": f"http://127.0.0.1:{port}", "model": "fake-vl",
                "api_key": "test-key", "retries": 0})

    # 直接测 read_screen
    print("[1] 单屏调用")
    img = np.full((400, 300, 3), 240, dtype=np.uint8)
    raw = vlm.read_screen(img, cfg, log=lambda *_: None)
    check("拿到 sections", bool(raw["sections"]), str(len(raw["sections"])))
    check("请求里带了图片", Handler.received and Handler.received[-1]["has_image"],
          str(Handler.received[-1] if Handler.received else None))
    check("请求里带了鉴权", bool(Handler.received[-1]["auth"]))
    check("记录了 token 用量", (raw.get("usage") or {}).get("prompt_tokens") == 1200,
          str(raw.get("usage")))

    # 再测完整读取流程
    print("\n[2] 完整逐屏读取")
    # 每屏画面略有不同，模拟"页面确实在滚动"，否则会被判定为"页面未移动"
    frames = []
    for i in range(3):
        f = np.full((1400, 2560, 3), 250, dtype=np.uint8)
        f[100 + i * 40: 140 + i * 40, :] = 200 - i * 20
        frames.append(f)
    state = {"i": 0}

    def fake_grab(rect):
        return frames[min(state["i"], len(frames) - 1)]

    def fake_scroll(x, y, c=0):
        # 真实滚动会让画面进入下一屏；测试里用滚动回调推进帧指针
        state["i"] = min(state["i"] + 1, len(frames) - 1)

    real_grab, real_scroll = winenv.grab_window, winenv.scroll
    try:
        winenv.grab_window = fake_grab
        winenv.scroll = fake_scroll
        resume_reader.time.sleep = lambda s: None
        Handler.call = 0
        rr = resume_reader.read_resume_vlm((0, 0, 2560, 1400), vision.Rect(1609, 252, 285, 40),
                                           log=lambda *_: None, max_steps=6, cfg=cfg)
    finally:
        winenv.grab_window, winenv.scroll = real_grab, real_scroll

    check("读取完成", rr.complete, rr.stopped_reason)
    check("读到姓名", rr.name == "测试候选人", repr(rr.name))
    check("读到教育经历", bool(rr.education) and rr.education[0]["school"] == "厦门大学",
          str(rr.education))
    check("工作经历 2 段", len(rr.work) == 2, str([w["company"] for w in rr.work]))
    check("跨屏合并补回公司名",
          any(w["company"] == "福州甲科技有限公司" for w in rr.work), str(rr.work[0]["company"]))
    check("职责合并", len(rr.work[0]["descriptions"]) >= 2, str(rr.work[0]["descriptions"]))
    check("reader 标记为 vlm", rr.reader == "vlm", rr.reader)
    check("完整性：没有缺失字段", not rr.missing, str(rr.missing))
    check("用量累计 >= 3 屏", rr.usage.get("screens", 0) >= 3, str(rr.usage))

    server.shutdown()
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
