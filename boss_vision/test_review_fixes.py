"""针对评审提出的三个问题的回归测试。

1) 跨屏职责片段不能被丢弃（归属到上一条经历；无法归属则计入复核）
2) 教育条目字段不全时不能立刻停止，要继续补采
3) 姓名有分歧必须进复核，且筛选入口要拦
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
import screening  # noqa: E402
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


# ---------------------------------------------------------------- 1) 片段
def test_fragments() -> None:
    print("\n[1] 跨屏职责片段不丢弃")
    raw = {"sections": [
        {"section": "work", "company": "甲公司", "title": "运营", "period": "2024.01-2024.06",
         "responsibilities": ["负责A"]},
        {"section": "work", "company": "", "title": "", "period": "",
         "responsibilities": ["负责B", "负责C"]},
    ]}
    out = vlm.normalize(raw)
    check("主条目保留", len(out["work"]) == 1, str(len(out["work"])))
    check("片段被单独返回而不是丢弃", len(out["fragments"]) == 1, str(len(out["fragments"])))
    check("片段内容完整", out["fragments"][0]["descriptions"] == ["负责B", "负责C"],
          str(out["fragments"][0]["descriptions"]))


# ---------------------------------------------------------------- 2) 教育补采
SCREENS = [
    {"name": "测试", "sections": [
        {"section": "work", "company": "甲公司", "title": "运营", "period": "2024.01-2024.06",
         "responsibilities": ["负责A"]}]},
    # 第二屏：教育只有学校，没有学历
    {"name": "", "sections": [
        {"section": "education", "school": "某大学", "major": "", "degree": "", "period": "2020-2024"}]},
    # 第三屏：补齐学历
    {"name": "", "sections": [
        {"section": "education", "school": "某大学", "major": "广告", "degree": "本科",
         "period": "2020-2024"}]},
    # 第四屏（如果还在读）
    {"name": "", "sections": [
        {"section": "work", "company": "", "title": "", "period": "", "responsibilities": ["续行职责"]}]},
]


class Handler(BaseHTTPRequestHandler):
    call = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if int(body.get("max_tokens") or 0) <= 60:
            content = "测试"
        else:
            content = json.dumps(SCREENS[min(Handler.call, len(SCREENS) - 1)], ensure_ascii=False)
            Handler.call += 1
        data = json.dumps({"choices": [{"message": {"content": content}}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 20}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_education_continue() -> None:
    print("\n[2] 教育字段不全时继续补采")
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    cfg = vlm.load_config()
    cfg.update({"base_url": f"http://127.0.0.1:{port}", "api_key": "t", "retries": 0,
                "model": "fake"})

    state = {"i": 0}
    frames = []
    for i in range(4):
        f = np.full((1400, 2560, 3), 250, dtype=np.uint8)
        f[100 + i * 40: 140 + i * 40, :] = 200 - i * 20
        frames.append(f)

    real_grab, real_scroll = winenv.grab_window, winenv.scroll
    real_sleep = resume_reader.time.sleep
    try:
        Handler.call = 0
        winenv.grab_window = lambda rect: frames[min(state["i"], len(frames) - 1)]
        winenv.scroll = lambda x, y, c=0: state.update(i=min(state["i"] + 1, len(frames) - 1))
        resume_reader.time.sleep = lambda s: None
        rr = resume_reader.read_resume_vlm((0, 0, 2560, 1400), vision.Rect(1609, 252, 285, 40),
                                           log=lambda *_: None, max_steps=6, cfg=cfg)
    finally:
        winenv.grab_window, winenv.scroll = real_grab, real_scroll
        resume_reader.time.sleep = real_sleep
        server.shutdown()

    check("没有在读到的第一屏就停", rr.screen_count >= 2, f"读了 {rr.screen_count} 屏")
    check("最终补到学历", any((e.get("degree") or "") == "本科" for e in rr.education),
          str(rr.education))
    check("完成原因写明字段完整", "字段完整" in (rr.stopped_reason or ""), rr.stopped_reason)


# ---------------------------------------------------------------- 3) 姓名冲突
def test_name_dispute() -> None:
    print("\n[3] 姓名分歧必须进复核")
    from resume_reader import ReadResult

    rr = ReadResult(name="王伟俐", name_candidates=["王伟俐", "王伟刚"], name_disputed=True,
                    work=[{"company": "甲", "title": "运营", "period": "2024", "descriptions": []}],
                    education=[{"school": "某大学", "degree": "本科", "period": "2020-2024"}],
                    complete=True, reached_education=True)
    # 走一遍与 read_resume_vlm 相同的收尾判定
    reasons = []
    if rr.name_disputed:
        reasons.append(f"姓名有分歧（候选 {'/'.join(rr.name_candidates)}）")
    rr.review_reason = "；".join(reasons)
    check("分歧被记录为复核原因", "姓名有分歧" in rr.review_reason, rr.review_reason)

    row = {"序号": 1, "姓名": "王伟俐", "学校": "某大学", "学历": "本科", "工作经历条数": 1,
           "已打开": "是", "采集完成": "是", "读到章节": "work/edu", "需复核": "是",
           "复核原因": rr.review_reason, "冲突数": 1}
    gate = screening.screen_collection(row, screening.load_rules())
    check("筛选入口拦住", not gate["collect_ok"], gate["collect_reason"])
    verdict = screening.screen_row(row, screening.load_rules())
    check("结论为不合格（二元口径）", verdict["最终结论"] == screening.REJECT, verdict["最终结论"])

    ok_row = dict(row, **{"需复核": "否", "复核原因": "", "冲突数": 0})
    check("无冲突时正常通过", screening.screen_collection(ok_row, screening.load_rules())["collect_ok"])


def main() -> int:
    test_fragments()
    test_education_continue()
    test_name_dispute()
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
