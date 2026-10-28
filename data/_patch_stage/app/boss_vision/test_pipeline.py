"""回归测试：不需要鼠标键盘注入，纯识别 + 几何 + 切片 + OCR。

    python test_pipeline.py            # 全部
    python test_pipeline.py --fast     # 跳过 OCR（约 7 秒）

每一项都对着 tests/fixtures 里两张真实截图断言，改代码后跑一遍就知道有没有碰坏。
"""
from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import driver
import extract
import resume_reader
import vision
import winenv

FIX = _HERE / "tests" / "fixtures"
LIST = FIX / "list.png"            # 推荐牛人列表（6 张完整卡片）
RESUME = FIX / "resume.png"        # 已打开的线上简历弹窗

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def load(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1].copy()


def test_list_geometry() -> None:
    print("\n[1] 列表页：按钮识别 + 卡片几何")
    img = load(LIST)
    boxes = vision.detect_teal(img)
    btns = vision.list_buttons(boxes)
    check("识别出 6 个「打招呼」按钮", len(btns) == 6, f"实际 {len(btns)}")
    check("全部是 92px 宽的小按钮", all(80 <= b.w <= 100 for b in btns),
          f"宽度 {sorted({b.w for b in btns})}")
    check("垂直节距约 184px",
          all(180 <= btns[i + 1].cy - btns[i].cy <= 190 for i in range(len(btns) - 1)),
          f"间距 {[btns[i + 1].cy - btns[i].cy for i in range(len(btns) - 1)]}")

    cards = [vision.card_for(b) for b in btns]
    check("卡片左边界 753", all(c.x == 753 for c in cards), f"实际 {sorted({c.x for c in cards})}")
    check("卡片高 172", all(c.h == 172 for c in cards))
    check("首卡顶部 221", cards[0].y == 221, f"实际 {cards[0].y}")

    first_click = vision.click_into_resume(btns[0])
    check("首卡落点 (1275, 300)", first_click == (1275, 300), f"实际 {first_click}")
    check("落点落在卡片内部",
          all(c.x < pt[0] < c.x + c.w and c.y < pt[1] < c.y + c.h
              for c, pt in zip(cards, [vision.click_into_resume(b) for b in btns])))
    check("落点不压在按钮上",
          all(not (b.x <= pt[0] <= b.x + b.w and b.y <= pt[1] <= b.y + b.h)
              for b, pt in zip(btns, [vision.click_into_resume(b) for b in btns])))


def test_resume_geometry() -> None:
    print("\n[2] 简历弹窗：大按钮 + 关闭 ✕ 锚点")
    img = load(RESUME)
    boxes = vision.detect_teal(img)
    wides = vision.wide_buttons(boxes)
    check("识别到 1 个 285x40 大按钮", len(wides) == 1 and wides[0].w == 285 and wides[0].h == 40,
          f"{[(w.x, w.y, w.w, w.h) for w in wides]}")
    if wides:
        cb = vision.close_button(wides[0])
        check("关闭 ✕ 落在 (1908, 168)", cb == (1908, 168), f"实际 {cb}")


def test_state_machine() -> None:
    print("\n[3] 页面状态判定")
    check("6 个小按钮 -> list",
          vision.guess_state([vision.Rect(0, i * 184, 92, 32) for i in range(6)], []) == "list")
    check("1 个大按钮 -> resume_modal",
          vision.guess_state([], [vision.Rect(1609, 252, 285, 40)]) == "resume_modal")
    check("1 个小按钮 -> list_partial",
          vision.guess_state([vision.Rect(0, 0, 92, 32)], []) == "list_partial")
    check("都没有 -> unknown", vision.guess_state([], []) == "unknown")


def test_sections() -> None:
    print("\n[4] 正文切片")
    text = (
        "期望职位 福州 外贸业务员\n工作经历\n福建飞鸟传媒有限公司 后期剪辑\n2023.06-至今\n"
        "主要负责公司项目的后期制作\n项目经历\n某项目\n教育经历\nEducation 广告学 本科\n岳阳学院\n自我评价\n认真"
    )
    secs = driver.slice_sections(text)
    check("切出「工作经历」", "工作经历" in secs)
    check("切出「教育经历」", "教育经历" in secs)
    check("工作经历在「项目经历」处截断", "项目经历" not in secs.get("工作经历", ""))
    check("教育经历在「自我评价」处截断", "自我评价" not in secs.get("教育经历", ""))


def test_ocr() -> None:
    print("\n[5] OCR（真跑模型，约 7 秒）")
    engine = vision.load_ocr()
    check("OCR 引擎可加载", engine is not None)
    if engine is None:
        return
    img = load(RESUME)
    items = vision.ocr_crop(engine, img, vision.Rect(800, 120, 800, 1270))
    joined = "".join(it["text"] for it in items)
    check("识别出足够多行", len(items) >= 40, f"{len(items)} 行")
    check("读到「工作经历」", "工作经历" in joined)
    check("读到「教育经历」", "教育经历" in joined)
    check("置信度分数可解析为 float",
          all(isinstance(it["score"], float) for it in items),
          f"样本 {items[0]['score']} ({type(items[0]['score']).__name__})")


def test_live_capture() -> None:
    print("\n[6] 实时窗口抓图")
    found = vision.winenv.find_window("BOSS直聘")
    if not found:
        print("  [SKIP] 没找到 BOSS 窗口（浏览器没开时跳过，不算失败）")
        return
    check("找到 BOSS 窗口", True, f"{found[2][:30]} {found[1]}")
    img = vision.winenv.grab_window(found[1])
    check("截图形状与窗口一致",
          (img.shape[1], img.shape[0]) == (found[1][2], found[1][3]),
          f"{img.shape[1]}x{img.shape[0]} vs {found[1][2]}x{found[1][3]}")
    p = driver.plan(img)
    check("能对实时画面出计划", p["state"] in {"list", "list_partial", "resume_modal", "unknown"},
          f"state={p['state']} cards={len(p['cards'])}")


def test_panel_band() -> None:
    print("\n[7] 弹窗正文识别区")
    img = load(RESUME)
    wide = vision.Rect(1609, 252, 285, 40)
    band = resume_reader.panel_band(img, wide)
    check("左边界 = 大按钮 x - 809 = 800", band.x == 800, f"实际 {band.x}")
    check("宽度 = 774", band.w == 774, f"实际 {band.w}")
    check("高度覆盖大半个窗口", band.h > 900, f"{band.h}")


def test_scroll_loop() -> None:
    print("\n[8] 滚动 + 逐屏拼接（用假引擎，不依赖 OCR / 不动机器）")
    frames = [
        ["张三刚刚活跃", "26岁 本科 在职-月内到岗", "工作经历",
         "福建飞鸟传媒有限公司 后期剪辑 2023.06 - 至今"],
        ["教育经历", "岳阳学院 广告 本科 2019 - 2023"],
    ]

    class FakeEngine:
        def __init__(self):
            self.calls = 0

        def __call__(self, bgr):
            lines = frames[min(self.calls, len(frames) - 1)]
            self.calls += 1
            # 模拟 rapidocr 1.x 的返回结构：(result, elapse)，每项 [四点框, 文本, 分数字符串]
            result = []
            for i, t in enumerate(lines):
                y0 = 20 + i * 30
                result.append([[[10, y0], [300, y0], [300, y0 + 25], [10, y0 + 25]], t, "0.9"])
            return result, None

    engine = FakeEngine()
    real_ocr, real_grab, real_scroll = vision.load_ocr, winenv.grab_window, winenv.scroll
    scrolled = []
    try:
        vision.load_ocr = lambda: engine
        winenv.grab_window = lambda rect: np.full((1400, 2560, 3), 250, dtype=np.uint8)
        winenv.scroll = lambda x, y, clicks=0: scrolled.append((x, y, clicks))

        rr = resume_reader.read_resume((0, 0, 2560, 1400), vision.Rect(1609, 252, 285, 40),
                                       log=lambda *_: None)
        check("触发了滚动", len(scrolled) >= 1, f"{len(scrolled)} 次")
        check("先把弹窗滚回顶部（正数向上）", scrolled and scrolled[0][2] > 0,
              f"第一次={scrolled[0][2] if scrolled else None}")
        check("之后都是向下滚", all(c < 0 for _, _, c in scrolled[1:]),
              f"{[c for _, _, c in scrolled[1:]]}")
        check("滚动点在正文区内", all(band_x_ok(x) for x, _, _ in scrolled),
              f"x={[x for x, _, _ in scrolled]}")
        check("标记已读到教育经历", rr.reached_education, rr.stopped_reason)
        check("切出教育经历小节", "教育经历" in rr.sections)
        check("两屏内容都保留", "工作经历" in rr.text and "岳阳学院" in rr.text)
        check("去重生效（无重复行）", len(rr.lines) == len({ln["text"] for ln in rr.lines}))
        check("识别到姓名", rr.name == "张三", repr(rr.name))
        check("提取到结构化工作经历", len(rr.work) >= 1,
              str([(w["company"], w["period"]) for w in rr.work]))
        check("提取到结构化教育经历", len(rr.education) >= 1,
              str([(e["school"], e["period"]) for e in rr.education]))

        # 第二组：滚了也没新内容 -> 应停下
        engine2 = FakeEngine()
        engine2.calls = 99  # 永远返回最后一帧
        vision.load_ocr = lambda: engine2
        scrolled.clear()
        rr2 = resume_reader.read_resume((0, 0, 2560, 1400), vision.Rect(1609, 252, 285, 40),
                                        log=lambda *_: None)
        check("无新内容时停止",
              rr2.stopped_reason.startswith(("读取完成", "页面未移动", "达到滚动上限")),
              rr2.stopped_reason)
    finally:
        vision.load_ocr, winenv.grab_window, winenv.scroll = real_ocr, real_grab, real_scroll


def band_x_ok(x: int) -> bool:
    """滚动点落在正文区 (800..1574) 内。"""
    return 800 <= x <= 1574


def test_dim_skip() -> None:
    print("\n[9] 淡化卡片跳过（只看文字深浅）")
    img = load(FIX / "dim_cards.png")
    p = driver.plan(img, icon_check=False)   # 关掉图标判据，单独验证"淡化"这条
    check("识别到 7 张卡片", len(p["cards"]) == 7, f"实际 {len(p['cards'])}")

    dim_idx = [i for i in range(len(p["cards"])) if p["skip_reason"][i] == "已沟通(淡化)"]
    check("前 3 张判为淡化", dim_idx == [0, 1, 2], f"实际 {dim_idx}")
    check("淡化卡深色占比都 < 0.12",
          all(p["stats"][i]["dark_ratio"] < 0.12 for i in dim_idx))
    check("正常卡深色占比都 > 0.2",
          all(p["stats"][i]["dark_ratio"] > 0.2 for i in (3, 4, 5)),
          str([p["stats"][i]["dark_ratio"] for i in (3, 4, 5)]))
    check("截断卡片也被跳过",
          any(r == "被截断" for r in p["skip_reason"]),
          str(p["skip_reason"]))
    check("待点击只剩 3 张", len(p["todo"]) == 3, f"实际 {p['todo']}")
    check("落点都在待处理卡上",
          all(p["skip"][i] is False for i in p["todo"]))


def test_icon_criteria() -> None:
    print("\n[9b] 「同事沟通进度」图标判据（与淡化并列）")
    img = load(FIX / "dim_cards.png")
    p = driver.plan(img, icon_check=True)
    check("7 张卡片都检测到图标",
          all(ic["pixels"] >= 25 for ic in p["icons"]),
          str([ic["pixels"] for ic in p["icons"]]))
    check("双判据下 7 张全部跳过", sum(p["skip"]) == 7, f"实际 {sum(p['skip'])}")
    check("待处理为空 -> 会走刷新分支", p["todo"] == [], str(p["todo"]))
    check("跳过原因含图标类",
          all("图标" in r or "进度" in r for r in p["skip_reason"]),
          str(p["skip_reason"]))
    # 关掉图标判据后应恢复成 3 个可点
    p2 = driver.plan(img, icon_check=False)
    check("--no-icon-check 时恢复 3 个可点", len(p2["todo"]) == 3, str(p2["todo"]))


def test_all_skipped_behavior() -> None:
    print("\n[10] 整屏 7 张全跳过：默认只向下滚，不刷新；开了开关才刷新")
    fake = {
        "state": "list",
        "buttons": [vision.Rect(1835, 244 + i * 184, 92, 32) for i in range(7)],
        "wides": [],
        "cards": [vision.Rect(753, 220 + i * 184, 1212, 172) for i in range(7)],
        "skip": [True] * 7,
        "skip_reason": ["有同事沟通进度"] * 6 + ["被截断"],
        "stats": [{"ink": 100, "dark_ratio": 0.05, "p10": 130, "dim": True}] * 7,
        "icons": [{"pixels": 172, "present": True, "score": 1.0}] * 7,
        "todo": [],
        "sigs": list(range(7)),
        "click": [],
    }
    real = (driver.plan, winenv.find_window, winenv.grab_window, winenv.focus,
            winenv.scroll, winenv.refresh_page, driver.time.sleep)
    try:
        driver.plan = lambda img, icon_check=True, text_scan=False: fake
        winenv.find_window = lambda kw="BOSS直聘": (1, (0, 0, 2560, 1400), "BOSS")
        winenv.grab_window = lambda rect: np.zeros((1400, 2560, 3), np.uint8)
        winenv.focus = lambda h: True
        driver.time.sleep = lambda s: None

        # A) 默认（allow_refresh=False）：只滚动，滚满就停
        acts: list[str] = []
        winenv.scroll = lambda x, y, c=0: acts.append(f"scroll{c}")
        winenv.refresh_page = lambda h, wait=0.3: (acts.append("refresh"), True)[1]
        rc = driver.run(live=True, limit=3, max_scrolls=2, read="none", anchor=(0, 0),
                        max_refresh=5, min_gap=0, max_gap=0, count_valid=True)
        check("默认只向下滚动", acts == ["scroll-3", "scroll-3"], str(acts))
        check("默认不刷新页面", "refresh" not in acts, str(acts))
        check("滚满后停止", rc == 0, str(rc))

        # B) 打开 allow_refresh：滚满后才会刷新
        acts.clear()
        rc = driver.run(live=True, limit=3, max_scrolls=0, read="none", anchor=(0, 0),
                        max_refresh=2, min_gap=0, max_gap=0, count_valid=True,
                        allow_refresh=True)
        check("允许刷新时才刷新", acts.count("refresh") == 2, str(acts))
        check("刷新次数用尽后停止", rc == 0, str(rc))
    finally:
        (driver.plan, winenv.find_window, winenv.grab_window, winenv.focus,
         winenv.scroll, winenv.refresh_page, driver.time.sleep) = real

def test_candidate_gap() -> None:
    print("\n[11] 候选人之间随机间隔（15~25s）+ 最后一个不等待")
    # 1) 单个函数：用虚拟时钟，避免真等 20 秒
    class FakeTime:
        def __init__(self):
            self.t = 1000.0
            self.slept: list[float] = []

        def time(self):
            return self.t

        def sleep(self, s):
            self.slept.append(s)
            self.t += s

    ft = FakeTime()
    real_time = driver.time
    try:
        driver.time = ft
        gap = driver.wait_between_candidates(15.0, 25.0)
        total = sum(ft.slept)
        check("函数返回的间隔在 15~25 之间", 15.0 <= gap <= 25.0, f"{gap:.2f}")
        check("累计睡眠 ≈ 返回的间隔", abs(total - gap) <= 0.55, f"{total:.2f} vs {gap:.2f}")
        check("等待被切成 ≤0.5s 的小片（便于响应停止）",
              all(s <= 0.5 + 1e-6 for s in ft.slept), f"最大片 {max(ft.slept):.2f}s")
        check("片数足够多（不是一次性睡死）", len(ft.slept) >= 30, f"{len(ft.slept)} 片")
    finally:
        driver.time = real_time

    # 2) 跑一轮：2 个候选人之间只调一次等待，最后一位之后不等待
    fake = {
        "state": "list",
        "buttons": [vision.Rect(1835, 244, 92, 32), vision.Rect(1835, 428, 92, 32)],
        "wides": [],
        "cards": [vision.Rect(753, 220, 1212, 172), vision.Rect(753, 404, 1212, 172)],
        "skip": [False, False],
        "skip_reason": ["", ""],
        "stats": [{"ink": 9, "dark_ratio": 0.3, "p10": 41, "dim": False}] * 2,
        "todo": [0, 1],
        "sigs": [11, 22],
        "icons": [{"pixels": 77, "present": True, "score": 0.46}] * 2,
        "click": [(1275, 300), (1275, 484)],
    }
    waits: list[tuple] = []
    real = (driver.plan, winenv.find_window, winenv.grab_window, winenv.focus,
            winenv.click, driver.time.sleep, driver.wait_between_candidates)
    try:
        driver.plan = lambda img, icon_check=True, text_scan=False: fake
        winenv.find_window = lambda kw="BOSS直聘": (1, (0, 0, 2560, 1400), "BOSS")
        winenv.grab_window = lambda rect: np.zeros((1400, 2560, 3), np.uint8)
        winenv.focus = lambda h: True
        winenv.click = lambda x, y: None
        driver.time.sleep = lambda s: None
        driver.wait_between_candidates = lambda mn, mx: (waits.append((mn, mx)), 0.0)[1]

        rc = driver.run(live=True, limit=2, max_scrolls=1, read="none", anchor=(0, 0),
                        max_refresh=1, min_gap=15.0, max_gap=25.0)
        check("两位候选人之间只等待 1 次", len(waits) == 1, str(waits))
        check("传入的是 15~25 区间", waits and waits[0] == (15.0, 25.0), str(waits))
        check("运行正常结束", rc == 0)
    finally:
        (driver.plan, winenv.find_window, winenv.grab_window, winenv.focus,
         winenv.click, driver.time.sleep, driver.wait_between_candidates) = real

def test_page_ready() -> None:
    print("\n[9c] 页面稳定性闸门（防刷新后骨架屏被误判）")

    def mk(ys, scores):
        return {"cards": [vision.Rect(753, y, 1212, 172) for y in ys],
                "icons": [{"score": s, "present": s >= 0.8} for s in scores]}

    a = mk([220, 410, 600], [1.0, 1.0, 0.46])
    b = mk([220, 410, 600], [1.0, 1.0, 0.46])
    c = mk([220, 410, 600], [-1.0, -1.0, -1.0])   # 骨架屏：图标还没渲染
    check("两帧一致 -> 稳定", driver.page_ready(b, a))
    check("图标得分变化 -> 未稳定", driver.page_ready(c, a) is False)
    check("没有卡片 -> 未稳定", driver.page_ready({"cards": [], "icons": []}) is False)


def test_close_modal() -> None:
    print("\n[9e] 关闭弹窗（回归：曾因 icon_check 未定义直接崩）")
    calls: list = []
    seq: list = []
    real = (winenv.click, winenv.press_key, winenv.grab_window, driver.plan)
    try:
        winenv.click = lambda x, y: calls.append(("click", x, y))
        winenv.press_key = lambda vk, hold=(0, 0): calls.append(("key", vk))
        winenv.grab_window = lambda rect: np.zeros((1400, 2560, 3), np.uint8)
        driver.plan = lambda img, icon_check=True: seq.pop(0)

        seq[:] = [{"state": "resume_modal"}, {"state": "list"}]
        ok = driver.close_modal((0, 0, 2560, 1400), (0, 0),
                                [vision.Rect(1609, 252, 285, 40)], icon_check=True)
        check("点 ✕ 后状态变为列表 -> 关闭成功", ok)
        check("第一次点的是锚点算出的 ✕ (1908,168)", calls[0] == ("click", 1908, 168), str(calls[:1]))

        calls.clear()
        seq[:] = [{"state": "resume_modal"}] * 4
        ok2 = driver.close_modal((0, 0, 2560, 1400), (0, 0),
                                 [vision.Rect(1609, 252, 285, 40)], tries=3, icon_check=True)
        check("一直关不掉 -> 返回 False", ok2 is False)
        check("三次尝试依次是 ✕ / 遮罩 / Esc",
              [c[0] for c in calls[:3]] == ["click", "click", "key"], str(calls))
    finally:
        winenv.click, winenv.press_key, winenv.grab_window, driver.plan = real


def test_icon_template() -> None:
    print("\n[9d] 人像图标靠模板匹配区分（不是数青色像素）")
    img = load(FIX / "icons_src.png")
    p = driver.plan(img, icon_check=True)
    check("识别到 6 张卡片", len(p["cards"]) == 6, f"实际 {len(p['cards'])}")
    has = [i for i, ic in enumerate(p["icons"]) if ic["present"]]
    check("只有 #1/#2/#3/#6 有人像图标", has == [0, 1, 2, 5], f"实际 {has}")
    check("有人像图标的得分 ≈1.0",
          all(p["icons"][i]["score"] > 0.95 for i in has),
          str([p["icons"][i]["score"] for i in has]))
    check("没有图标的得分明显低",
          all(p["icons"][i]["score"] < 0.6 for i in (3, 4)),
          str([p["icons"][i]["score"] for i in (3, 4)]))
    check("待点击正好是 #4/#5", p["todo"] == [3, 4], str(p["todo"]))


def test_hot_block_skip() -> None:
    print("\n[9f] 「热搜牛人推荐」内嵌块：整块不点")
    img = load(FIX / "hot_block.png")
    p = driver.plan(img, icon_check=True)
    check("识别到 1 个内嵌推荐块", len(p["blocks"]) == 1, str(p["blocks"]))
    if p["blocks"]:
        z = p["blocks"][0]
        check("块区域覆盖标题带（y≈417~667）", z.y <= 430 and z.h >= 200, f"y={z.y} h={z.h}")
        check("块宽度接近列表宽度", 900 <= z.w <= 1200, f"w={z.w}")
        check("块内没有卡片被当成待点击",
              all(not (z.y <= c.cy <= z.y + z.h) for i, c in enumerate(p["cards"]) if i in p["todo"]),
              str(p["todo"]))
    labels = p.get("block_labels") or []
    check("块标题通过 OCR 确认", bool(labels) and any(
        "牛人推荐" in (lb.get("text") or "") or lb.get("is_hot") for lb in labels),
        str([lb.get("text") for lb in labels]))
    # 再造一张"落在块里"的卡片，确认会被跳过
    fake_card = vision.Rect(753, 500, 1212, 172)
    check("落在块内的卡片判定为命中",
          vision.inside_any(fake_card, p["blocks"]) is not None,
          f"card.cy={fake_card.cy}")

def test_textfix() -> None:
    print("\n[9g] OCR 错字表（corrections.json）")
    import textfix

    check("已知错字被修正",
          textfix.fix_text("省直职工子女暑署托班") == "省直职工子女暑托班",
          textfix.fix_text("省直职工子女暑署托班"))
    check("正常文本不受影响", textfix.fix_text("厦门市一器之言电子商务有限公司") == "厦门市一器之言电子商务有限公司")
    fixed = textfix.fix_obj({"work": [{"company": "省直职工子女暑署托班"}]})
    check("嵌套结构里也会修正", fixed["work"][0]["company"] == "省直职工子女暑托班", str(fixed))
    check("数字等其它类型不动", textfix.fix_obj({"n": 3, "ok": True}) == {"n": 3, "ok": True})
    check("对照表文件存在", textfix.ensure_file().exists())


def test_name_and_section_guards() -> None:
    print("\n[9h] 姓名上下文识别 + 小节标题不能当公司名")
    import resume_reader

    cases = {
        "招聘规\n肖紫阳\n目刚刚活跃\n27岁丨大专16年1离职-随时到岗\n": "肖紫阳",
        "招聘规\n陈龙\n刚刚活跃\n24岁丨大专丨3年丨离职-随时到岗\n": "陈龙",
        "招聘规\n噢噢·在线\n23岁丨本科11年离职-随时到岗\n": "噢噢",
        "招聘规\n阙宏涛  刚刚活跃\n21岁丨本科丨27年应届生\n": "阙宏涛",
        "招聘规\n吴沁雪\n今日活跃\n21岁丨大专丨1年丨离职-随时到岗\n": "吴沁雪",
    }
    for text, want in cases.items():
        got = resume_reader.guess_name_from_text(text)
        check(f"文本认姓名 {want}", got == want, f"实际 {got!r}")

    rows = [{"text": "工作经历 2025.02 -2026.09", "score": .9, "box": [0, 0, 1, 1]}]
    entry = extract.parse_work_entry(rows, [[{"text": "·维护社群用户", "score": .9, "box": [0, 0, 1, 1]}]])
    check("小节标题不会被当成公司名", entry.company == "", repr(entry.company))

    edu_row = [{"text": "教育经历 本科 2021-2025", "score": .9, "box": [0, 0, 1, 1]}]
    edu = extract.parse_edu_entry(edu_row, [])
    check("小节标题不会被当成学校名", edu.school == "", repr(edu.school))



def test_daily_table() -> None:
    print("\n[9i] 按日期命名的汇总表（每次采集自动累计）")
    import export_csv

    day_dir = Path(__file__).parent / "_test_runs" / "daily"
    day_dir.mkdir(parents=True, exist_ok=True)
    for old in day_dir.glob("*.csv"):
        old.unlink()

    rec1 = {"index": 1, "name": "张三", "opened": True, "steps": 1, "stopped_reason": "已读到教育经历",
            "work": [{"company": "A公司", "title": "运营", "period": "2024.01-至今",
                      "descriptions": ["干A"], "tags": []}],
            "education": [{"school": "X学院", "major": "电商", "degree": "本科", "period": "2020-2024"}]}
    rec2 = {"index": 2, "name": "李四", "opened": True, "steps": 1,
            "work": [{"company": "B公司", "title": "助理", "period": "2023.01-2024.01",
                      "descriptions": ["干B"], "tags": []}], "education": []}

    path1 = export_csv.merge_into_daily(day_dir, [export_csv.row_of(rec1)])
    check("表名是日期", path1 is not None and path1.name == export_csv.daily_path(day_dir).name,
          path1.name if path1 else "None")
    check("文件名形如 YYYY-MM-DD.csv",
          bool(path1) and len(path1.stem) == 10 and path1.stem[4] == "-", path1.stem if path1 else "")

    # 再采一次：新增一人 + 重复张三 -> 去重后应 2 条
    export_csv.merge_into_daily(day_dir, [export_csv.row_of(rec1), export_csv.row_of(rec2)])
    files = list(day_dir.glob("*.csv"))
    check("同一天只有一张表", len(files) == 1, str([f.name for f in files]))

    with files[0].open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    check("两次采集累计成 2 条（重复的已去重）", len(rows) == 2, f"实际 {len(rows)}")
    check("表头含第1家公司", "第1家公司" in rows[0], str(list(rows[0])[:6]))
    check("姓名都在", [r["姓名"] for r in rows] == ["张三", "李四"], str([r["姓名"] for r in rows]))
    check("序号连续（1..N）", [r["序号"] for r in rows] == ["1", "2"], str([r["序号"] for r in rows]))

    # 再采一次（index 又从 1 开始）-> 序号应接着排 3
    rec3 = {"index": 1, "name": "王五", "opened": True, "steps": 1,
            "work": [{"company": "C公司", "title": "运营", "period": "2024.01-至今",
                      "descriptions": ["x"], "tags": []}], "education": []}
    export_csv.merge_into_daily(day_dir, [export_csv.row_of(rec3)])
    with files[0].open(encoding="utf-8-sig", newline="") as fh:
        rows2 = list(csv.DictReader(fh))
    check("跨批次后序号继续连续", [r["序号"] for r in rows2] == ["1", "2", "3"],
          str([r["序号"] for r in rows2]))

    # 换一天 -> 直接建新表，序号从 1 开始
    other = export_csv.merge_into_daily(day_dir, [export_csv.row_of(rec1)], day="2099-01-01")
    check("第二天生成新表", other is not None and other.name == "2099-01-01.csv",
          other.name if other else "None")
    if other:
        with other.open(encoding="utf-8-sig", newline="") as fh:
            other_rows = list(csv.DictReader(fh))
        check("新表序号从 1 开始", [r["序号"] for r in other_rows] == ["1"], str([r["序号"] for r in other_rows]))
        other.unlink()

    for f in files:
        f.unlink()




def test_strict_header_and_dedupe() -> None:
    print("\n[9j] 描述里的日期不算条目头 + 重复经历去重")
    section = "\n".join([
        "工作经历",
        "福州翌厉商贸有限公司丨跨境电商运营",
        "2025.05-至今",
        "业绩：",
        "累计创作20余条播放量达10万以上的优质视频，有效提升产品曝光度与用户转化率。",
        "2021.09 -2023.02",                     # 描述正文里出现的日期
        "福建承创堂中药有限公司丨商务专员实习",
        "2020.07 - 2020.11",
        "使用千牛聊天工具接待客户。",
        "福建承创堂中药有限公司丨商务专员",
        "2020.07 -2020.11",                     # 同一段经历被两帧重复采集（时间空格不同）
        "【实习",
        "2018.07 - 2018.09",
        "执行广告投放需求。",
        "天悦海信息科技（福州）有限公司丨媒介投放",
    ])
    works = extract.parse_section_text(section, "work")
    companies = [w.company for w in works if w.company]
    check("描述里的日期不会被当成新条目", all("累计创作" not in c for c in companies), str(companies))
    check("重复的同名经历只留一条", companies.count("福建承创堂中药有限公司") == 1, str(companies))
    check("公司名不为空且不是垃圾字符", all(len(c.strip(" 【】·-")) >= 2 for c in companies), str(companies))
    check("识别的公司数量合理（2~3 家）", 2 <= len(companies) <= 3, str(companies))



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过 OCR")
    a = ap.parse_args()

    print("BOSS 招聘端画面识别 —— 回归测试")
    # 测试产生的 results.json 不要污染真实的 runs/ 目录
    driver.OUT = Path(__file__).parent / "_test_runs"
    driver.OUT.mkdir(exist_ok=True)
    test_list_geometry()
    test_resume_geometry()
    test_state_machine()
    test_sections()
    test_panel_band()
    test_scroll_loop()
    test_dim_skip()
    test_icon_criteria()
    test_page_ready()
    test_close_modal()
    test_hot_block_skip()
    test_textfix()
    test_name_and_section_guards()
    test_daily_table()
    test_strict_header_and_dedupe()
    test_icon_template()
    test_all_skipped_behavior()
    test_candidate_gap()
    if not a.fast:
        test_ocr()
    test_live_capture()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, d) for n, ok, d in results if not ok]
    print(f"\n{'=' * 50}\n{passed}/{len(results)} 通过")
    for name, detail in failed:
        print(f"  FAIL: {name} — {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
