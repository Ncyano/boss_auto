"""云端服务：任务调度 + 代理通道 + 视觉识别 + 结果看板。

设计要点：
    · 云端只做识别与判定；浏览器操作全部由本地代理执行（截图/滚动/点击）
    · 截图读完即丢，不落盘（只持久化结构化结果）
    · 分辨率无关：一切坐标用 CSS 像素，按 meta.ratio 换算

启动：
    uvicorn cloud.server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

# 极简鉴权：口令来自环境变量（生产建议换 JWT）
SHARED_TOKEN = os.environ.get("BOSS_TOKEN", "change-me")
CANON_W = 1280
KEEP_SHOT_ON_ERROR = os.environ.get("KEEP_SHOT_ON_ERROR", "0") == "1"

app = FastAPI(title="BOSS 采集云端", version="1.0")


# ---------------------------------------------------------------- 数据模型
@dataclass
class Task:
    id: str
    position: str
    target: int
    min_gap: float
    max_gap: float
    created: float
    status: str = "pending"          # pending / running / done / stopped / error
    handled: int = 0
    valid: int = 0
    rejected: int = 0
    agent: str = ""                  # 绑定的代理 id
    results: list[dict] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "position": self.position, "target": self.target,
            "min_gap": self.min_gap, "max_gap": self.max_gap,
            "status": self.status, "handled": self.handled, "valid": self.valid,
            "rejected": self.rejected, "agent": self.agent, "error": self.error,
            "created": self.created,
        }


TASKS: dict[str, Task] = {}


class NewTask(BaseModel):
    position: str = "运营助理"
    target: int = 20
    min_gap: float = 4.0
    max_gap: float = 7.0


# ---------------------------------------------------------------- 代理通道
@dataclass
class Agent:
    id: str
    ws: WebSocket
    meta: dict          # 最近一次 geometry
    task_id: str = ""
    last_seen: float = field(default_factory=time.time)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _id: int = 0

    async def call(self, cmd: str, timeout: float = 30.0, **payload) -> dict:
        """下发命令并等待结果。"""
        self._id += 1
        mid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        await self.ws.send_text(json.dumps({"id": mid, "cmd": cmd, **payload},
                                           ensure_ascii=False))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(mid, None)

    def resolve(self, msg: dict) -> None:
        mid = msg.get("id")
        fut = self.pending.get(int(mid) if mid is not None else -1)
        if fut and not fut.done():
            fut.set_result(msg)


AGENTS: dict[str, Agent] = {}


def pick_agent(task: Task) -> Agent | None:
    if task.agent and task.agent in AGENTS:
        return AGENTS[task.agent]
    if AGENTS:
        # 未指定则取最近活跃的一个
        return max(AGENTS.values(), key=lambda a: a.last_seen)
    return None


# ---------------------------------------------------------------- 视觉识别
SYSTEM_PROMPT = """你在读 BOSS 直聘的候选人简历弹窗截图。
只读中间的简历内容，忽略顶部导航、右上角账号名、左侧菜单、地址栏。
姓名只取简历面板内的候选人姓名；确定不了就留空字符串，绝不能填招聘者账号名。
只输出 JSON：
{"name":"","age":"","education_mode":"","graduation_year":"","status":"","expect":"",
 "work":[{"company":"","title":"","period":"","descriptions":[]}],
 "education":[{"school":"","major":"","degree":"","period":""}]}"""


def _vision(bgr, cfg: dict) -> dict:
    """调用视觉模型（同步，放线程里跑）。"""
    import sys
    sys.path.insert(0, str(ROOT.parent / "boss_vision"))
    import vlm  # 复用本项目的请求封装
    return vlm.ask_vision(SYSTEM_PROMPT, bgr, cfg)


def _decode_jpeg(b64: str):
    import numpy as np
    import cv2
    raw = base64.b64decode(b64)
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("截图解码失败")
    return img


# ---------------------------------------------------------------- 采集编排
async def run_task(task: Task) -> None:
    """简化版编排：截图 → 识别 → 判定 → （可选）点击打招呼 → 滚下一位。"""
    agent = pick_agent(task)
    if not agent:
        task.status, task.error = "error", "没有已连接的本地代理"
        return
    task.agent, task.status = agent.id, "running"
    meta = agent.meta or {}

    import sys
    sys.path.insert(0, str(ROOT.parent / "boss_vision"))
    import judge
    import vlm
    import screening
    import judge_inline

    cfg = dict(vlm.load_config())
    cfg["max_image_side"] = CANON_W

    try:
        while task.status == "running" and task.valid < task.target:
            shot = await agent.call("screenshot", timeout=40)
            if not shot.get("ok"):
                task.error = shot.get("error", "")
                break
            meta = shot.get("meta") or meta
            img = _decode_jpeg(shot["data"])
            shot["data"] = None              # 图片用完即丢

            data = await asyncio.to_thread(_vision, img, cfg)
            obj = data.get("data") or {}
            del img                          # 显式释放，不留存

            rec = {
                "name": str(obj.get("name") or "").strip(),
                "work": obj.get("work") or [],
                "education": obj.get("education") or [],
                "basic": {k: obj.get(k) for k in
                          ("age", "education_mode", "graduation_year", "status", "expect")},
                "complete": bool(obj.get("work") and obj.get("education")),
                "reached_education": bool(obj.get("education")),
                "missing": [],
            }
            row = judge_inline.record_to_row(rec)
            out = screening.screen_row(row, screening.load_rules())
            verdict = out.get("最终结论")
            task.handled += 1
            if verdict == "合适":
                task.valid += 1
            else:
                task.rejected += 1

            task.results.append({
                "name": rec["name"] or "(无名)",
                "verdict": verdict,
                "status": out.get("处理状态"),
                "reason": out.get("原因"),
                "work": len(rec["work"]),
                "edu": len(rec["education"]),
                "at": time.time(),
            })
            _persist(task)

            if verdict == "合适":
                await agent.call("click", timeout=15,
                                 x=int(meta.get("css_w", 0) * 0.86),
                                 y=int(meta.get("css_h", 0) * 0.12))
                await asyncio.sleep((task.min_gap + task.max_gap) / 2)

            # 滚一屏继续
            await agent.call("scroll", timeout=15,
                             x=int(meta.get("css_w", 0) // 2),
                             y=int(meta.get("css_h", 0) // 2),
                             dy=600)
        if task.status == "running":
            task.status = "done"
    except Exception as exc:
        task.status, task.error = "error", f"{type(exc).__name__}: {exc}"
    finally:
        _persist(task)


def _persist(task: Task) -> None:
    day = time.strftime("%Y%m%d")
    d = DATA / "results" / day
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task.id}.json").write_text(
        json.dumps({**task.to_dict(), "results": task.results}, ensure_ascii=False, indent=2),
        encoding="utf-8")


# ---------------------------------------------------------------- API
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.post("/api/tasks")
async def create_task(body: NewTask) -> dict:
    t = Task(id=uuid.uuid4().hex[:8], position=body.position, target=body.target,
             min_gap=body.min_gap, max_gap=body.max_gap, created=time.time())
    TASKS[t.id] = t
    return t.to_dict()


@app.get("/api/tasks")
async def list_tasks() -> list[dict]:
    return [t.to_dict() for t in TASKS.values()]


@app.get("/api/tasks/{tid}")
async def get_task(tid: str) -> dict:
    t = TASKS.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {**t.to_dict(), "results": t.results}


@app.post("/api/tasks/{tid}/start")
async def start_task(tid: str) -> dict:
    t = TASKS.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.status == "running":
        raise HTTPException(400, "任务已在运行")
    t.status = "running"
    asyncio.create_task(run_task(t))
    return {"ok": True, "id": tid}


@app.post("/api/tasks/{tid}/stop")
async def stop_task(tid: str) -> dict:
    t = TASKS.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    t.status = "stopped"
    return {"ok": True}


@app.get("/api/tasks/{tid}.csv")
async def export_csv(tid: str) -> StreamingResponse:
    t = TASKS.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["序号", "姓名", "结论", "处理状态", "原因", "工作条数", "教育条数", "时间"])
    for i, r in enumerate(t.results, 1):
        w.writerow([i, r.get("name"), r.get("verdict"), r.get("status"),
                    r.get("reason"), r.get("work"), r.get("edu"),
                    time.strftime("%H:%M:%S", time.localtime(r.get("at", 0)))])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{tid}.csv"'})


@app.get("/api/agents")
async def list_agents() -> list[dict]:
    return [{"id": a.id, "task": a.task_id, "meta": a.meta,
             "age": round(time.time() - a.last_seen, 1)} for a in AGENTS.values()]


@app.websocket("/agent")
async def agent_ws(ws: WebSocket) -> None:
    token = ws.query_params.get("token", "")
    if token != SHARED_TOKEN:
        await ws.close(code=4001)
        return
    await ws.accept()
    aid = uuid.uuid4().hex[:8]
    ag = Agent(id=aid, ws=ws, meta={})
    AGENTS[aid] = ag
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            ag.last_seen = time.time()
            if msg.get("event") == "hello":
                ag.meta = msg.get("meta") or {}
                continue
            if msg.get("meta"):
                ag.meta = msg["meta"]
            ag.resolve(msg)
    except WebSocketDisconnect:
        pass
    finally:
        AGENTS.pop(aid, None)


@app.get("/healthz")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "agents": len(AGENTS), "tasks": len(TASKS)})
