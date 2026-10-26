"""编排层：把「看画面 -> 点开简历 -> 读 -> 关闭 -> 滚动」串成循环。

默认 **dry-run（只算不点）**：只输出它*打算*点的坐标，并生成标注图。
真正动鼠标必须显式 --live，且受 --limit 限制。

    python driver.py plan                  # 看它准备点哪（只读）
    python driver.py run --live --limit 1  # 真点 1 个人
    python driver.py run --live --limit 5 --read clip
"""
from __future__ import annotations

import argparse
import atexit
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
_DEPS = _HERE / ".deps"
if _DEPS.is_dir():
    sys.path.insert(0, str(_DEPS))

import vision
import winenv

OUT = _HERE / "runs"

# ---- GUI 用的协作式控制 ----
STOP = False                                  # GUI 点"停止"时置 True
PROGRESS = {"handled": 0, "valid": 0, "target": 0, "state": "idle"}


def _fmt_duration(seconds: float) -> str:
    """把秒数格式化成 1:23 / 1:02:03。"""
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def request_stop() -> None:
    global STOP
    STOP = True


def reset_control() -> None:
    global STOP
    STOP = False

SECTION_START = ("工作经历", "教育经历")
SECTION_END = ("项目经历", "资格证书", "求职意向", "自我评价", "附件", "举报", "不合适")


class _TeeStream:
    """把输出同时写到原流和日志文件；任一侧失败都不影响另一侧。"""

    def __init__(self, first, log_file):
        self.first = first
        self.log_file = log_file

    def write(self, s):
        for target in (self.first, self.log_file):
            try:
                target.write(s)
                target.flush()
            except Exception:
                pass

    def flush(self):
        for target in (self.first, self.log_file):
            try:
                target.flush()
            except Exception:
                pass


def plan(img: np.ndarray, icon_check: bool = True, text_scan: bool = False) -> dict:
    boxes = vision.detect_teal(img)
    btns = vision.list_buttons(boxes)
    wides = vision.wide_buttons(boxes)
    cards = [vision.card_for(b) for b in btns]

    # 三条判据：① 文字淡化  ② 有「同事沟通进度」图标  ③ 落在「热搜牛人推荐」块里
    h_img = img.shape[0]
    strips = vision.find_special_blocks(img)
    zones = vision.block_zones(strips, cards, h_img)
    labels: list[dict] = []

    # 纯文字模式：颜色找不到时（或配色变了）用 OCR 再兜一次
    if text_scan and not zones:
        zones = vision.find_block_by_text(img, cards)
    if strips:
        labels = vision.read_block_labels(img, strips)     # 确认块标题（热搜牛人推荐）

    skips, stats, icons, reasons = [], [], [], []
    for card, btn in zip(cards, btns):
        dim, st = vision.is_contacted(img, card)
        icon = vision.progress_icon(img, btn)
        partial = (card.y + card.h) > h_img - 6 or btn.h > 60  # 被屏幕底边截断
        skip_icon = icon_check and icon["present"]
        in_block = vision.inside_any(card, zones) is not None
        skips.append(dim or skip_icon or partial or in_block)
        stats.append(st)
        icons.append(icon)
        if in_block:
            reasons.append("热搜牛人推荐")
        elif dim and skip_icon:
            reasons.append("已沟通(淡化+图标)")
        elif dim:
            reasons.append("已沟通(淡化)")
        elif skip_icon:
            reasons.append("有同事沟通进度")
        elif partial:
            reasons.append("被截断")
        else:
            reasons.append("")

    todo = [i for i, s in enumerate(skips) if not s]
    sigs = [vision.card_signature(img, c) for c in cards]
    return {
        "state": vision.guess_state(btns, wides),
        "buttons": btns,
        "wides": wides,
        "cards": cards,
        "skip": skips,
        "skip_reason": reasons,
        "stats": stats,
        "icons": icons,
        "blocks": zones,
        "block_labels": labels,
        "todo": todo,
        "sigs": sigs,
        "click": [vision.click_into_resume(btns[i]) for i in todo],
    }


def annotate(img: np.ndarray, p: dict, tag: str = "plan") -> Path:
    ann = Image.fromarray(img[:, :, ::-1])
    d = ImageDraw.Draw(ann)
    for i, (card, btn) in enumerate(zip(p["cards"], p["buttons"])):
        if p["skip"][i]:
            # 跳过项：灰框 + 原因，不画落点
            d.rectangle([card.x, card.y, card.x + card.w, card.y + card.h], outline=(150, 150, 150), width=3)
            d.text((card.x + 12, card.y + 10),
                   f"#{i + 1} SKIP {p['skip_reason'][i]} dark={p['stats'][i]['dark_ratio']}",
                   fill=(120, 120, 120))
            continue
        pt = vision.click_into_resume(btn)
        d.rectangle([card.x, card.y, card.x + card.w, card.y + card.h], outline=(0, 90, 255), width=3)
        d.rounded_rectangle([btn.x - 3, btn.y - 3, btn.x + btn.w + 3, btn.y + btn.h + 3], radius=6,
                            outline=(255, 0, 0), width=3)
        d.ellipse([pt[0] - 16, pt[1] - 16, pt[0] + 16, pt[1] + 16], outline=(255, 0, 0), width=4)
        d.line([pt[0] - 26, pt[1], pt[0] + 26, pt[1]], fill=(255, 0, 0), width=2)
        d.line([pt[0], pt[1] - 26, pt[0], pt[1] + 26], fill=(255, 0, 0), width=2)
        d.text((card.x + 12, card.y + 10), f"#{i + 1}", fill=(255, 0, 0))
    for z in p.get("blocks", []):
        d.rectangle([z.x, z.y, z.x + z.w, z.y + z.h], outline=(200, 0, 200), width=3)
        d.text((z.x + 8, z.y + 6), "special block -> skip", fill=(200, 0, 200))
    for w in p["wides"]:
        d.rectangle([w.x, w.y, w.x + w.w, w.y + w.h], outline=(255, 0, 255), width=3)
        d.text((w.x + 8, max(0, w.y - 18)), "greet(in modal)", fill=(255, 0, 255))
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    ann.save(path)
    return path


def slice_sections(text: str) -> dict[str, str]:
    """把整页文本切成「工作经历 / 教育经历」。"""
    out: dict[str, str] = {}
    for sec in SECTION_START:
        i = text.find(sec)
        if i < 0:
            continue
        j = len(text)
        for end in SECTION_END:
            k = text.find(end, i + len(sec))
            if k > i:
                j = min(j, k)
        out[sec] = text[i:j].strip()
    return out


def read_modal_text(origin: tuple[int, int], anchor: tuple[int, int]) -> tuple[str, dict[str, str]]:
    """在简历弹窗内：点正文 -> Ctrl+A -> Ctrl+C -> 读剪贴板。"""
    ox, oy = origin
    ax, ay = anchor
    winenv.click(ox + ax, oy + ay)
    time.sleep(0.35)
    winenv.hotkey(winenv.VK_CONTROL, winenv.VK_A)
    time.sleep(0.3)
    winenv.hotkey(winenv.VK_CONTROL, winenv.VK_C)
    time.sleep(0.6)
    text = winenv.clipboard_text()
    return text, slice_sections(text)


def close_modal(rect: tuple[int, int, int, int], origin: tuple[int, int],
                wides: list[vision.Rect], tries: int = 3,
                icon_check: bool = True) -> bool:
    """关闭简历弹窗。

    实测 Esc 无效。顺序：点锚点算出的 ✕ -> 点左侧遮罩 -> Esc，每次点完都复检状态。
    """
    ox, oy = origin
    cands: list[tuple[int, int]] = []
    if wides:
        cands.append(vision.close_button(wides[0]))
    cands.append((int(rect[2] * 0.15), int(rect[3] * 0.5)))
    for i in range(tries):
        try:
            if i < len(cands):
                cx, cy = cands[i]
                print(f"     关闭弹窗：点 ({cx},{cy})")
                winenv.click(ox + cx, oy + cy)
            else:
                print("     关闭弹窗：Esc")
                winenv.press_key(winenv.VK_ESCAPE)
            time.sleep(0.9)
            if plan(winenv.grab_window(rect), icon_check)["state"] != "resume_modal":
                return True
        except winenv.InputBlocked:
            raise                       # 注入被拦属于环境问题，直接上报
        except Exception as exc:        # 抓图/识别偶发异常不该终止整轮
            print(f"     关闭动作异常：{type(exc).__name__}: {exc}")
            time.sleep(0.5)
    return False


def wait_between_candidates(min_gap: float, max_gap: float) -> float:
    """两个候选人之间的随机间隔（默认 15~25 秒）。

    计时起点 = 上一个候选人的动作全部结束之后。
    目前动作是「打开简历 -> 滚动读完 -> 关闭弹窗」；
    **打招呼实装后**把 greet 插进这套动作里，间隔自然就从打招呼完成之后开始算，
    不需要改这里。
    """
    gap = random.uniform(min_gap, max_gap)
    print(f"     等待 {gap:.1f} 秒再处理下一个（{min_gap:.0f}~{max_gap:.0f}s 随机）")
    end = time.time() + gap
    while time.time() < end:
        if STOP:                      # 停止按钮：立刻结束等待
            print("     收到停止指令，取消等待")
            break
        time.sleep(min(0.5, max(0.0, end - time.time())))
    return gap


def frame_key(p: dict) -> tuple:
    """一帧画面的"指纹"：每张卡的 y 位置 + 人像图标匹配得分。

    用来判断页面是否渲染稳定 —— 连续两帧一致才认为可以点；骨架屏阶段会一直变。
    """
    icons = p.get("icons") or [{}] * len(p["cards"])
    return tuple((c.y, round(float(ic.get("score", -1)), 2))
                 for c, ic in zip(p["cards"], icons))


def page_ready(p: dict, prev: dict | None = None) -> bool:
    """页面是否已渲染稳定：至少有卡片，且与上一帧完全一致。"""
    if not p.get("cards"):
        return False
    if prev is None:
        return True
    return frame_key(p) == frame_key(prev)


def log_cards(p: dict) -> None:
    """逐张打印判定依据，方便事后核对"为什么点了/为什么跳过"。"""
    for i, card in enumerate(p["cards"]):
        st = p["stats"][i]
        ic = p.get("icons", [{}] * len(p["cards"]))[i]
        act = f"跳过({p['skip_reason'][i]})" if p["skip"][i] else "会点击"
        score = ic.get("score", -1)
        print(f"    #{i + 1} y={card.y:<4} dark={st['dark_ratio']:.3f} "
              f"人像得分={score:>5.3f} -> {act}")


def run(live: bool, limit: int, max_scrolls: int | None = None, read: str = "scroll",
        anchor: tuple[int, int] | None = None,
        max_refresh: int | None = None, min_gap: float = 3.0, max_gap: float = 5.0,
        icon_check: bool = True, count_valid: bool = False,
        max_attempts: int | None = None, allow_refresh: bool = False,
        block_text_scan: bool = False, reader: str = "auto") -> int:
    winenv.enable_dpi_awareness()
    found = winenv.find_window("BOSS直聘")
    if not found:
        print("没找到标题含「BOSS直聘」的窗口，请先把 BOSS 页面打开。")
        return 2
    hwnd, rect, title = found
    ox, oy = rect[0], rect[1]
    print(f"窗口: {title}")
    print(f"位置: ({ox},{oy}) 尺寸: {rect[2]}x{rect[3]}")
    print(f"模式: {'LIVE（会真实点击）' if live else 'DRY-RUN（只算不点）'}")

    # 权限体检：注入被 UIPI 拦是最常见的"点了没反应"，先把两边的完整性级别打出来
    if live:
        ok, msg = winenv.injection_permission(hwnd)
        print(("  ✓ " if ok else "  ✗ ") + msg)
        if not ok:
            print("    → 解决办法：右键 run-gui.cmd / 终端，选择「以管理员身份运行」后重试")

    if live:
        winenv.focus(hwnd)
        time.sleep(0.4)

    started_at = time.time()
    stamps = time.strftime("%Y%m%d-%H%M%S")
    OUT.mkdir(exist_ok=True)
    results: list[dict] = []
    _seen_people: set = set()          # 已采集过的候选人指纹（去重）
    duplicate_skipped = 0              # 因重复被跳过的次数
    # CDP 坐标换算需要窗口在屏幕上的位置/尺寸（分辨率无关）
    try:
        import winenv as _we, cdp as _cdp
        if _we._cdp_mode():
            _cdp._CDP_INSTANCE = None
            c = _cdp.CDP()
            c.set_window_rect(rect)
            print(f"  CDP 标定：窗口 {c.calibrated['win_px']} / 页面 {c.calibrated['page_css']}"
                  f" CSS / 缩放 {c.calibrated['scale']}（dpr={c.calibrated['dpr']}）")
            _we._CDP_CLIENT[0] = c
    except Exception:
        pass
    modal_open = [False]               # 当前是否有简历弹窗开着（统一清理用）
    handled = 0
    valid = 0                    # 真正打开并读到内容的次数（"有效人次"）
    PROGRESS.update({"handled": 0, "valid": 0, "target": limit, "state": "running"})
    scrolls = 0
    refreshes = 0
    no_progress = 0              # 连续"没有新候选人"的次数（不设滚动上限时的守卫）
    NO_PROGRESS_LIMIT = 40       # 连续 40 次滚动都没有新人 -> 认为到底
    modal_fails = 0
    render_fails = 0            # 页面渲染未完成、刷新的次数
    seen_sigs: set[int] = set()   # 已处理过的卡片指纹，防刷新/回滚后重复处理

    own_write: list[float] = []          # 记录本进程上次写文件的时间，用于识别"被别人覆盖"

    def save_shots(shots: list, index: int) -> None:
        """只对"需复核"的候选人把逐屏截图落盘，方便事后肉眼核对。"""
        if not shots:
            return
        try:
            from PIL import Image

            folder = OUT / f"{stamps}-shots"
            folder.mkdir(parents=True, exist_ok=True)
            for screen, arr in shots:
                path = folder / f"{index:02d}-{screen:02d}.png"
                Image.fromarray(arr[:, :, ::-1]).save(path)
            print(f"     已保存 {len(shots)} 张逐屏截图 -> {folder}")
        except Exception as exc:
            print(f"     [warn] 截图保存失败：{exc}")


    last_record_at: list[float] = []


    def dump_results(with_csv: bool = True) -> None:
        # 距最后一次记录超过 30 分钟 -> 这是"很久以前跑完的进程在退出时回写"，直接跳过
        if last_record_at and (time.time() - last_record_at[0]) > 1800:
            print("[跳过] 本进程的结果已过期（超过 30 分钟未更新），不再回写")
            return
        """把已处理的结果落盘：results.json + 带工作内容的 summary.csv。

        注册到 atexit，所以 Ctrl+C / 点停止中断时也会把已有结果写出来。
        """
        try:
            OUT.mkdir(exist_ok=True)
            json_path = OUT / f"{stamps}-results.json"
            if json_path.exists():
                mtime = json_path.stat().st_mtime
                ours = own_write[0] if own_write else None
                if ours is not None and abs(mtime - ours) > 0.001:
                    print(f"[跳过] {json_path.name} 已被其他进程改写，保留较新的那份")
                    return
            # 原始层不做任何文本替换：错字表只在导出 CSV / 筛选时套用，原文永远可追溯
            json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            own_write[:] = [json_path.stat().st_mtime]
            print(f"\n结果已写入 {json_path}（{len(results)} 条）")
        except Exception as exc:  # 落盘失败不能影响退出
            print(f"[warn] 结果写入失败：{exc}")
            return

        if not results or not with_csv:
            return
        try:
            import export_csv   # 同目录模块：行结构 + CSV 写法保持一致

            try:
                import textfix

                rows = [export_csv.row_of(textfix.fix_obj(rec)) for rec in results]
            except Exception:
                rows = [export_csv.row_of(rec) for rec in results]
            rows = export_csv.dedupe_rows(rows)      # 同一人只留一条
            export_csv.write_csv(OUT / f"{stamps}-summary.csv", rows)
            export_csv.merge_into_daily(OUT, rows)   # 当天那张表：按日期命名并累计
        except Exception as exc:
            print(f"[warn] CSV 生成失败：{exc}")

    atexit.register(dump_results)          # 退出/中断时写 json + csv

    # 把这一轮的控制台输出同时写进 runs\<stamp>-console.log，事后可查
    log_path = OUT / f"{stamps}-console.log"
    OUT.mkdir(exist_ok=True)
    _log_file = open(log_path, "w", encoding="utf-8")
    _orig_stdout = sys.stdout

    _orig_stderr = sys.stderr
    sys.stdout = _TeeStream(_orig_stdout, _log_file)
    sys.stderr = _TeeStream(_orig_stderr, _log_file)   # 异常/traceback 也进日志
    def _log_excepthook(exc_type, exc, tb):
        import traceback

        print("\n[未捕获异常] 程序中断，traceback：")
        traceback.print_exception(exc_type, exc, tb, file=sys.stdout)

    sys.excepthook = _log_excepthook
    print(f"日志文件: {log_path}")

    if max_attempts is None:
        max_attempts = max(limit * 5, limit + 10)

    def reached() -> bool:
        """是否已达目标：count_valid 时只算真正打开简历的，否则算尝试次数。"""
        return (valid if count_valid else handled) >= limit

    while not reached() and handled < max_attempts and not STOP:
        # 统一清理：上一轮若有分支没关弹窗（重复/异常），这里先补关一次，避免卡在简历页
        if modal_open[0]:
            print("  上一轮弹窗未关闭，先关闭再继续")
            try:
                if close_modal(rect, (ox, oy), plan(winenv.grab_window(rect), icon_check)["wides"],
                               icon_check=icon_check):
                    modal_open[0] = False
                    print("     已回到列表")
                else:
                    print("     关不掉弹窗，停止。")
                    return 3
            except Exception as exc:
                print(f"     [warn] 关闭失败：{exc}")
                return 3

        img = winenv.grab_window(rect)
        p = plan(img, icon_check, block_text_scan)
        print(f"\n[state={p['state']}] 卡片 {len(p['cards'])} 个")

        # 页面就绪闸门：等画面稳定（连续两帧一致）再判定。
        # 骨架屏阶段图标/文字都还没渲染，此时判"没有图标"会误点。
        if p["state"] == "list" and p["cards"]:
            prev = p
            waited = 0.0
            stable = False
            for _ in range(8):
                time.sleep(0.6)
                waited += 0.6
                p = plan(winenv.grab_window(rect), icon_check)
                if frame_key(p) == frame_key(prev):
                    stable = True
                    break
                prev = p
            if stable:
                if waited >= 1.2:
                    print(f"  等页面稳定用了 {waited:.1f}s")
            else:
                if not live:
                    print("  dry-run：页面一直在变（未稳定），结束")
                    break
                if not allow_refresh:
                    print("  页面一直不稳定，但刷新已禁用 -> 停止")
                    break
                render_fails += 1
                if render_fails > max_refresh:
                    print(f"  连续 {render_fails - 1} 次都没稳定 -> 停止（页面卡住或注入被拦）")
                    break
                print(f"  页面未稳定 -> 刷新重来，本轮不点击（第 {render_fails} 次）")
                winenv.refresh_page(hwnd)
                time.sleep(3.0)
                continue

        if p["state"] == "resume_modal":
            if not live:
                print("  当前停在简历弹窗：dry-run 不做任何事")
                break
            if not close_modal(rect, (ox, oy), p["wides"], icon_check=icon_check):
                modal_fails += 1
                print(f"  关不掉弹窗（第 {modal_fails} 次）")
                if modal_fails >= 2:
                    print("  请手动关闭弹窗后重跑。")
                    return 3
            continue

        if not p["cards"]:
            scrolls += 1
            print("  没识别到卡片 -> 滚动")
            if not live:
                break
            winenv.scroll(ox + rect[2] // 2, oy + rect[3] // 2, -3)
            time.sleep(0.9)
            continue

        skipped = sum(1 for s in p["skip"] if s)
        if skipped:
            print(f"  跳过 {skipped} 张（文字淡化 / 有同事沟通进度图标 / 被截断 / 内嵌推荐块）")
        for lb in p.get("block_labels") or []:
            txt = lb.get("text") or "(标题未识别)"
            print(f"  内嵌推荐块：{txt}  {'→ 整块跳过' if lb.get('is_hot') else '→ 也按块跳过'}")
        log_cards(p)


        # 选出「可点且没处理过」的候选；每次只处理一个，处理完回到循环顶部重新截图。
        # 打开/关闭简历后列表会重排，沿用旧坐标点下一个容易点空（这也是"只跑一条就停"的隐患）。
        sigs = p.get("sigs") or [0] * len(p["cards"])
        picked = None
        dup = 0
        for k, idx in enumerate(p["todo"]):
            if sigs[idx] and sigs[idx] in seen_sigs:
                dup += 1
                continue
            picked = (idx, p["click"][k])
            break
        if dup:
            print(f"  {dup} 张是已处理过的候选人 -> 跳过")

        if picked is None:
            no_progress += 1
            if no_progress == 10:
                try:
                    winenv.disable_cdp_scroll()
                    print("  连续 10 次滚动都拿不到新卡片，本次运行改用真实滚轮；"
                          "点击仍走 CDP")
                except Exception:
                    pass
            # 不设硬上限（max_scrolls=None），但连续无进展太多说明真没人了 -> 优雅停止
            if max_scrolls is None and no_progress > NO_PROGRESS_LIMIT:
                print(f"  连续 {no_progress} 次滚动都没拿到新候选人 -> 判断列表已到底，停止")
                break
            if max_scrolls is None or scrolls < max_scrolls:
                scrolls += 1
                cap = "不限" if max_scrolls is None else max_scrolls
                print(f"  本屏没有可点的新卡片 -> 向下滚一屏（{scrolls}/{cap}）")
                if not live:
                    print(f"  dry-run：不动作。标注图 {annotate(img, p)}")
                    return 0
                winenv.scroll(ox + rect[2] // 2, oy + rect[3] // 2, -3)
                time.sleep(0.9)
                continue
            if not allow_refresh:
                print(f"  已向下滚动 {scrolls} 屏，没有新的可点卡片（刷新已禁用）-> 停止")
                break
            if max_refresh is not None and refreshes >= max_refresh:
                print(f"  滚动 {scrolls} 次 + 刷新 {refreshes} 次都没拿到可点卡片 -> 停止")
                break
            refreshes += 1
            cap2 = "不限" if max_refresh is None else max_refresh
            print(f"  本屏没有可点的新卡片 -> 刷新页面（第 {refreshes}/{cap2} 次）")
            if not live:
                print(f"  dry-run：不刷新。标注图 {annotate(img, p)}")
                return 0
            before = [c.y for c in p["cards"]]
            if not winenv.refresh_page(hwnd):
                print("  ✗ 刷新失败：无法把 BOSS 窗口切到前台（F5 只会发给前台窗口）")
                print("    请手动点一下 BOSS 页面让它成为前台窗口，或把 GUI 最小化后重试")
                max_refresh = 0              # 刷新失败就不再重试，交给上层停止
                continue
            time.sleep(3.0)
            pa = plan(winenv.grab_window(rect), icon_check)
            if [c.y for c in pa["cards"]] == before:
                print("  警告：刷新前后卡片位置没变，可能是页面还没重载完（再等 2 秒）")
                time.sleep(2.0)
                pa = plan(winenv.grab_window(rect), icon_check)
            print(f"  刷新后：卡片 {len(pa['cards'])} 张，跳过 {sum(1 for s in pa['skip'] if s)} 张")
            continue

        for idx, (cx, cy) in [picked]:
            if reached():          # 注意：用 reached()，不能再用 handled >= limit
                break
            card = p["cards"][idx]
            sig = (p.get("sigs") or [0] * len(p["cards"]))[idx]
            if sig and sig in seen_sigs:
                print(f"  - 卡片 #{idx + 1} 与已处理过的候选人重复 -> 跳过（刷新后回到顶部会遇到）")
                continue
            if sig:
                seen_sigs.add(sig)
            print(f"  -> 点开卡片 #{idx + 1}（第 {handled + 1} 个候选人）: "
                  f"窗口内({cx},{cy}) / 屏幕({ox + cx},{oy + cy})")
            if not live:
                print(f"  dry-run：不点击。标注图 {annotate(img, p)}")
                return 0

            if STOP:
                print("  收到停止指令，不再点击")
                break

            # 卡片姓名必须在点开前读（点开后再截屏会读到简历正文）
            pre_card_name = ""
            try:
                import resume_reader as _rr_pre
                import vlm as _vlm_pre
                card_idx_pre = picked[0] if isinstance(picked, tuple) else picked
                _nm = _rr_pre.card_name(winenv.grab_window(rect), p["cards"][card_idx_pre])
                if _nm and _vlm_pre.is_plausible_name(_nm):
                    pre_card_name = _nm
            except Exception as exc:
                print(f"     [warn] 卡片姓名读取失败：{exc}")

            winenv.click(ox + cx, oy + cy)
            time.sleep(1.0)
            opened = False
            p2 = None
            for _ in range(8):
                p2 = plan(winenv.grab_window(rect), icon_check)
                if p2["state"] == "resume_modal":
                    opened = True
                    modal_open[0] = True
                    break
                time.sleep(0.5)
            print(f"     弹窗已打开: {opened}")

            record = {"index": handled + 1, "click": [ox + cx, oy + cy], "opened": opened}
            if opened and read == "scroll" and p2 and p2["wides"]:
                import resume_reader

                try:
                    card_name = (pre_card_name or "").strip()
                    if card_name:
                        print(f"     卡片姓名: {card_name}")

                    mode = reader
                    if mode == "auto":
                        try:
                            import vlm
                            mode = "vlm" if vlm.available() else "ocr"
                        except Exception:
                            mode = "ocr"
                    if mode == "vlm":
                        import vlm as _vlm_cfg
                        _fast = bool(_vlm_cfg.load_config().get("fast"))
                        _fn = (resume_reader.read_resume_fast if _fast
                               else resume_reader.read_resume_vlm)
                        rr = _fn(rect, p2["wides"][0], log=print, name_hint=card_name)
                    else:
                        rr = resume_reader.read_resume(rect, p2["wides"][0], log=print)

                    # 采集必须完整：缺字段就整段重读（回顶再读），最多 2 次
                    # 注意：不能只看"缺失项更少"——读到的条目更少时缺失项也会变少，
                    # 必须用"信息量"综合比较，避免把好结果覆盖成空结果。
                    def _richness(res) -> tuple:
                        work = getattr(res, "work", []) or []
                        edu = getattr(res, "education", []) or []
                        duties = sum(len(w.get("descriptions") or []) for w in work)
                        named = sum(1 for w in work if (w.get("company") or "").strip())
                        edu_ok = 1 if (edu and (edu[0].get("school") or "").strip()
                                       and (edu[0].get("degree") or "").strip()) else 0
                        return (named, duties, len(work), edu_ok,
                                -len(getattr(res, "missing", []) or []))

                    best = rr
                    seen_missing: set = set()
                    fast_mode = bool(getattr(best, "reader", "") == "vlm-fast")
                    if fast_mode:
                        print("     快速读法：跳过整段回顶重读（缺失项保留为提示）")
                        retry = -1
                    for retry in range(0 if fast_mode else 2):
                        missing = list(getattr(best, "missing", []) or [])
                        if not missing:
                            break
                        key_missing = tuple(sorted(missing))
                        if key_missing in seen_missing:
                            print(f"     缺失项与上次相同（{'；'.join(missing[:3])}），"
                                  f"不再整段重读")
                            break
                        seen_missing.add(key_missing)
                        print(f"     ⚠ 采集缺失：{'；'.join(missing[:4])}，"
                              f"回顶重读 {retry + 1}/2")
                        try:
                            if mode == "vlm":
                                rr2 = resume_reader.read_resume_vlm(rect, p2["wides"][0],
                                                                    log=print,
                                                                    name_hint=card_name)
                            else:
                                rr2 = resume_reader.read_resume(rect, p2["wides"][0], log=print)
                            if _richness(rr2) > _richness(best):
                                print(f"     重读结果更好，采用"
                                      f"（工作 {len(rr2.work)} 条 / 缺失 {len(rr2.missing)} 项）")
                                best = rr2
                            else:
                                print(f"     重读结果更差（工作 {len(rr2.work)} 条 / "
                                      f"缺失 {len(rr2.missing)} 项），保留原结果")
                                break
                        except Exception as exc:
                            print(f"     [warn] 重读失败：{exc}")
                            break
                    rr = best
                    record.update({
                        "name": rr.name,
                        "reader": getattr(rr, "reader", "ocr"),
                        "usage": getattr(rr, "usage", {}),
                        "text_len": len(rr.text),
                        "sections": rr.sections,
                        "steps": rr.steps,
                        "reached_education": rr.reached_education,
                        "stopped_reason": rr.stopped_reason,
                        "work": rr.work,
                        "education": rr.education,
                        "complete": bool(rr.complete),
                        "at_top": bool(rr.at_top),
                        "screen_count": rr.screen_count,
                        "section_seen": rr.section_seen,
                        "ocr_lines": rr.raw_lines,
                        "sidebar_entries": rr.sidebar,
                        "conflicts": rr.conflicts,
                        "basic": getattr(rr, "basic", {}) or {},
                        "missing": list(getattr(rr, "missing", []) or []),
                        "work_state": getattr(rr, "work_state", "unknown"),
                        "work_evidence": getattr(rr, "work_evidence", ""),
                        "name_candidates": list(getattr(rr, "name_candidates", []) or []),
                        "name_issues": list(getattr(rr, "name_issues", []) or []),
                        "name_disputed": bool(getattr(rr, "name_disputed", False)),
                        "needs_review": bool(rr.incomplete),
                        "review_reason": rr.review_reason,
                    })
                    if rr.incomplete:
                        print(f"     ⚠ 需复核：{rr.review_reason}")
                    # 截图：默认只在需复核时留（省磁盘）；config.vision.save_shots="always" 时全留
                    try:
                        import vlm as _vlm_s
                        _cfg_s = _vlm_s.load_config()
                        if rr.shots and (rr.incomplete or
                                         str(_cfg_s.get("save_shots") or "").lower() == "always"):
                            save_shots(rr.shots, handled + 1)
                    except Exception:
                        if rr.shots and rr.incomplete:
                            save_shots(rr.shots, handled + 1)
                    # 弹窗里的姓名不合法（抓到标签/职位）时，改用列表卡片上的姓名
                    try:
                        import vlm as _vlm
                        if card_name and not _vlm.is_plausible_name(rr.name or ""):
                            if rr.name:
                                print(f"     姓名不可用（{rr.name}），改用卡片姓名 {card_name}")
                            rr.name = card_name
                            rr.review_reason = ((rr.review_reason + "；") if rr.review_reason else "") + \
                                "姓名取自列表卡片"
                    except Exception:
                        pass

                    if not rr.name and card_name:
                        rr.name = card_name
                        rr.review_reason = ((rr.review_reason + "；") if rr.review_reason else "") + \
                            "姓名取自列表卡片"
                    elif rr.name and card_name and rr.name != card_name:
                        rr.name_issues.append(
                            f"姓名不一致：卡片 {card_name} / 简历 {rr.name}")
                    record["card_name"] = card_name
                    print(f"     {rr.name or '(未识别姓名)'}：滚了 {rr.steps} 屏，读到 {len(rr.text)} 字；"
                          f"工作经历 {len(rr.work)} 条 / 教育经历 {len(rr.education)} 条；"
                          f"停止原因={rr.stopped_reason}")
                    (OUT / f"{stamps}-resume-{handled + 1}.txt").write_text(rr.text, encoding="utf-8")
                except RuntimeError as exc:  # OCR 没装
                    print(f"     [{exc}] 退回剪贴板方式")
                    text, secs = read_modal_text((ox, oy), anchor)
                    record.update({"text_len": len(text), "sections": secs, "read": "clip-fallback"})
                    (OUT / f"{stamps}-resume-{handled + 1}.txt").write_text(text, encoding="utf-8")
            elif opened and read == "clip":
                text, secs = read_modal_text((ox, oy), anchor)
                record["text_len"] = len(text)
                record["sections"] = secs
                print(f"     读到正文 {len(text)} 字；切出小节 {list(secs)}")
                (OUT / f"{stamps}-resume-{handled + 1}.txt").write_text(text, encoding="utf-8")
            # 去重：同一个人被重复点开时（滚动后卡片位置变了会重复出现），只保留第一次
            try:
                def _fp(rec: dict) -> tuple:
                    edu = (rec.get("education") or [{}])[0]
                    work = (rec.get("work") or [{}])[0]
                    return (
                        (rec.get("name") or "").strip(),
                        (edu.get("school") or "").strip(),
                        (work.get("company") or "").strip(),
                        (work.get("period") or "").replace(" ", ""),
                    )

                fingerprint = _fp(record)

                def _similar(a: str, b: str) -> bool:
                    """姓名差一个字、学校/公司相同也算同一人。"""
                    if not a or not b:
                        return a == b
                    if a == b:
                        return True
                    if abs(len(a) - len(b)) > 1:
                        return False
                    import difflib as _dl
                    return _dl.SequenceMatcher(None, a, b).ratio() >= 0.66

                _dup_of = None
                for _old in _seen_people:
                    if _similar(fingerprint[0], _old[0]) and (
                            fingerprint[1] == _old[1] or fingerprint[2] == _old[2]):
                        _dup_of = _old
                        break

                if _dup_of is not None:
                    # 同一人：先关掉弹窗（否则下一轮会卡在简历页），再跳过
                    print(f"     ↺ {record.get('name')} 与之前重复"
                          f"（{fingerprint[1] or '无学校'}），关闭弹窗后跳过")
                    duplicate_skipped += 1
                    try:
                        if close_modal(rect, (ox, oy), p["wides"], icon_check=icon_check):
                            modal_open[0] = False
                        else:
                            print("     关不掉弹窗，停止。")
                            return 3
                    except Exception as exc:
                        print(f"     [warn] 关闭弹窗失败：{exc}")
                    continue
                if any(fingerprint):
                    _seen_people.add(fingerprint)
            except Exception as exc:
                print(f"     [warn] 去重检查失败：{exc}")

            # 判定放到后台线程：与"关弹窗 + 候选人间隔"并行，串行等待变成重叠
            judgment_box: dict = {}

            def _judge_async(rec: dict, box: dict) -> None:
                try:
                    import judge_inline
                    box["value"] = judge_inline.judge_record(rec)
                except Exception as exc:
                    box["value"] = {"verdict": "不合适", "status": "调用失败",
                                    "reason": f"判定异常：{exc}"[:120], "stage": "error"}

            import threading
            worker = threading.Thread(target=_judge_async, args=(record, judgment_box),
                                      daemon=True)
            worker.start()

            if opened and not close_modal(
                rect, (ox, oy), plan(winenv.grab_window(rect), icon_check)["wides"],
                icon_check=icon_check,
            ):
                print("  关不掉弹窗，停止。")
                worker.join(timeout=30)
                record["judgment"] = judgment_box.get("value") or {}
                (OUT / f"{stamps}-results.json").write_text(
                    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                return 3
            modal_open[0] = False
            print("     已回到列表")
            handled += 1
            no_progress = 0        # 采到人了，无进展计数归零
            if opened:
                valid += 1
            PROGRESS.update({"handled": handled, "valid": valid})
            print(f"     进度：有效 {valid}/{limit}（尝试 {handled} 次）")

            # 候选人之间的随机间隔：判定线程在这段时间里跑完，不额外占用时间
            gap = 0.0
            if not reached():
                gap = wait_between_candidates(min_gap, max_gap)

            worker.join(timeout=60)          # 判定最迟 60 秒返回
            verdict = judgment_box.get("value") or {
                "verdict": "不合适", "status": "调用失败",
                "reason": "判定超时（60 秒未返回）", "stage": "error"}
            record["judgment"] = verdict
            status = verdict.get("status") or "正常"
            flag = "" if status == "正常" else f"，{status}"
            print(f"     ➜ 判定：{verdict['verdict']}"
                  f"（{verdict['stage']}层{flag}｜{verdict['reason'][:60]}）")

            record["gap_before_next"] = round(gap, 1)
            results.append(record)
            last_record_at[:] = [time.time()]
            dump_results(with_csv=False)   # 增量只写 json，csv 在结束时统一生成

        # 处理完一个就回到循环顶部重新截图、重新判定（不再沿用旧坐标）
        continue

    (OUT / f"{stamps}-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    PROGRESS.update({"handled": handled, "valid": valid,
                     "state": "stopped" if STOP else "done"})
    tag = "已停止" if STOP else "完成"
    # 判定汇总
    try:
        verdicts = {}
        for rec in results:
            v = (rec.get("judgment") or {}).get("verdict")
            if v:
                verdicts[v] = verdicts.get(v, 0) + 1
        if verdicts:
            parts = "，".join(f"{k} {v}" for k, v in verdicts.items())
            if duplicate_skipped:
                print(f"重复跳过：{duplicate_skipped} 次（同一人重复出现）")
            print(f"判定汇总：{parts}")
            for rec in results:
                j = rec.get("judgment") or {}
                if j.get("verdict"):
                    print(f"  {rec.get('name') or '(无名)':<10} {j['verdict']:<4}"
                          f"（{j.get('stage', '')}层）{j.get('reason', '')[:50]}")
    except Exception as exc:
        print(f"[warn] 判定汇总失败：{exc}")

    duration = time.time() - started_at
    print(f"\n{tag}：尝试 {handled} 次，有效 {valid} 个（目标 {limit}）")
    print(f"总耗时：{_fmt_duration(duration)}"
          + (f"（平均 {_fmt_duration(duration / valid)}/人）" if valid else ""))
    print(f"结果目录：{OUT}")
    print(f"  {stamps}-results.json   结构化结果")
    print(f"  {stamps}-summary.csv    汇总表（含工作内容）")
    print(f"  {stamps}-resume-*.txt   每人简历全文")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "plan"])
    ap.add_argument("--live", action="store_true", help="真的点击（默认只算不点）")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--max-scrolls", type=int, default=None,
                    help="最多向下滚几屏；不填=不限制（连续无进展会自动停）")
    ap.add_argument("--max-refresh", type=int, default=None,
                    help="整屏卡片全跳过时，最多刷新页面几次换新候选人")
    ap.add_argument("--block-text-scan", dest="block_text_scan", action="store_true",
                    help="用 OCR 文字找内嵌推荐块（不依赖配色，较慢 1.5~3s/帧）")
    ap.add_argument("--reader", choices=("auto", "ocr", "vlm"), default="auto",
                    help="简历识别方式：auto=有 VLM key 就用大模型，否则 OCR")
    ap.add_argument("--allow-refresh", dest="allow_refresh", action="store_true",
                    help="整屏都跳过/滚动用尽时允许刷新页面（默认只往下滚，不刷新）")
    ap.add_argument("--no-icon-check", dest="icon_check", action="store_false",
                    help="不检测「同事沟通进度」图标，只按文字淡化判断")
    ap.add_argument("--min-gap", type=float, default=3.0,
                    help="两个候选人之间的最小间隔秒数（默认 3）")
    ap.add_argument("--max-gap", type=float, default=5.0,
                    help="两个候选人之间的最大间隔秒数（默认 5）")
    ap.add_argument("--read", choices=["none", "clip", "scroll"], default="scroll",
                    help="读取方式：scroll=弹窗内滚动逐屏 OCR（默认，能读全教育经历）；clip=Ctrl+A 复制")
    ap.add_argument("--anchor", default="1250,600", help="clip 方式下正文里的安全落点（窗口内坐标）")
    a = ap.parse_args()
    anchor = tuple(int(v) for v in a.anchor.split(","))

    if a.cmd == "plan":
        winenv.enable_dpi_awareness()
        found = winenv.find_window("BOSS直聘")
        if not found:
            print("没找到 BOSS 窗口")
            return 2
        _hwnd, rect, _title = found
        img = winenv.grab_window(rect)
        p = plan(img)
        print(f"state={p['state']} cards={len(p['cards'])} wide={len(p['wides'])} "
              f"跳过={sum(1 for s in p['skip'] if s)} 待处理={len(p['todo'])}")
        for i, card in enumerate(p["cards"]):
            st = p["stats"][i]
            flag = "SKIP 已沟通" if p["skip"][i] else "待处理"
            pt = "" if p["skip"][i] else f" click={vision.click_into_resume(p['buttons'][i])}"
            print(f"  #{i + 1} y={card.y:<4} {flag:<10} dark={st['dark_ratio']:.3f} p10={st['p10']}{pt}")
        print(f"标注图 {annotate(img, p)}")
        return 0

    try:
        return run(a.live, a.limit, a.max_scrolls, a.read, anchor, a.max_refresh,
                   a.min_gap, a.max_gap, a.icon_check, allow_refresh=a.allow_refresh,
                   block_text_scan=a.block_text_scan, reader=a.reader)
    except KeyboardInterrupt:
        print("\n[已中断] 收到 Ctrl+C：已处理的候选人和结果都已保存，可以安全关闭。")
        return 130
    except winenv.InputBlocked as exc:
        print(f"\n[已中止] {exc}")
        print("没有发生任何点击。请在普通终端 / PyCharm 里运行（不要用受限沙箱或提权终端）。")
        return 4


if __name__ == "__main__":
    sys.exit(main())
