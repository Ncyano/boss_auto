"""BOSS 招聘端「画面识别」层。

设计原则
--------
* 只看屏幕，不读 DOM：窗口抓图 -> 颜色/形状/文字识别 -> 输出元素相对坐标。
* 本文件不含任何点击代码。点击在 driver.py，且必须显式 --live。
* 进程启动即声明 DPI 感知；坐标以「窗口左上角」为原点。

用法
----
    python vision.py scan --ocr          # 抓一帧、识别、存标注图 + json
    python vision.py scan --screen       # 不用窗口，直接抓整屏（调试用）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

_DEPS = Path(__file__).parent / ".deps"
if _DEPS.is_dir():
    sys.path.insert(0, str(_DEPS))

import winenv

OUT = Path(__file__).parent / "runs"

# 青绿色「打招呼」按钮的判据（BOSS 主色，两套截图实测稳定）
TEAL = dict(r_max=110, g_min=140, b_min=130, rg_gap=60, br_gap=50)


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


def _runs(mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    br = np.flatnonzero(np.diff(idx) > 1)
    s = np.r_[idx[0], idx[br + 1]]
    e = np.r_[idx[br], idx[-1]]
    return [(int(a), int(b)) for a, b in zip(s, e) if b - a + 1 >= min_len]


def detect_teal(img: np.ndarray, min_area: int = 400) -> list[Rect]:
    b, g, r = (img[:, :, i].astype(int) for i in range(3))
    mask = (
        (r < TEAL["r_max"])
        & (g > TEAL["g_min"])
        & (b > TEAL["b_min"])
        & (g - r > TEAL["rg_gap"])
        & (b - r > TEAL["br_gap"])
    )
    boxes: list[Rect] = []
    for y0, y1 in _runs(mask.any(axis=1), 8):
        band = mask[y0 : y1 + 1]
        for x0, x1 in _runs(band.any(axis=0), 8):
            sub = mask[y0 : y1 + 1, x0 : x1 + 1]
            if sub.sum() >= min_area:
                boxes.append(Rect(x0, y0, x1 - x0 + 1, y1 - y0 + 1))
    return boxes


def list_buttons(boxes: list[Rect]) -> list[Rect]:
    """列表卡片右侧的「打招呼」小按钮（实测 92x32；宽度 <80 的多为文字高亮，需排除）。"""
    return sorted([b for b in boxes if 80 <= b.w <= 170 and 26 <= b.h <= 60], key=lambda b: b.cy)


def wide_buttons(boxes: list[Rect]) -> list[Rect]:
    """简历弹窗侧栏里的「打招呼」大按钮（实测 285x40）。"""
    return [b for b in boxes if b.w >= 200 and 25 <= b.h <= 70]


# ---- 尺度自适应 ----
# 所有偏移都是按 2560x1400 窗口、100% 缩放实测的。
# 运行时用检测到的按钮宽度反推缩放系数 k，换分辨率 / 改 DPI 时几何关系依然成立。
REF_BUTTON_W = 92.0
REF_WIDE_W = 285.0


def scale_of(actual: float, ref: float = REF_BUTTON_W) -> float:
    """按实测宽度推算缩放系数（限制在 0.3~3 之间，防止异常值）。"""
    if not actual or not ref:
        return 1.0
    return max(0.3, min(3.0, float(actual) / float(ref)))


def card_for(button: Rect, card_w: int = 1212, top_offset: int = 39) -> Rect:
    """由「打招呼」按钮反推卡片区域。

    实测（2560x1400 窗口）：按钮贴卡片右缘（右缘再外扩 38px），卡片高 172px，
    按钮垂直中心比卡片顶边低 39px。全部用**按钮相对偏移**表达，卡片位置变了也不影响。
    """
    k = scale_of(button.w)
    left = max(0, button.x + button.w + int(round(38 * k)) - int(round(card_w * k)))
    return Rect(left, max(0, button.cy - int(round(top_offset * k))), int(round(card_w * k)),
                int(round(172 * k)))


def click_into_resume(button: Rect) -> tuple[int, int]:
    """点开线上简历的落点：卡片中部偏左的「时间/经历」文字区，避开按钮与公司名链接。"""
    k = scale_of(button.w)
    return button.x - int(round(560 * k)), button.cy + int(round(40 * k))


def close_button(wide: Rect) -> tuple[int, int]:
    """简历弹窗右上角的 ✕ 关闭按钮。

    实测：相对侧栏大「打招呼」按钮的右上角偏移 (+14, -84)，即 (1908, 168)。
    Esc 在 BOSS 这个弹窗上无效，必须点这个 ✕ 或遮罩。
    """
    return wide.x + wide.w + 14, wide.y - 84


# 已联系过的卡片文字是淡灰色。实测：
#   淡化卡：深色(<100)占比 0.04~0.06，p10 亮度 ≥121
#   正常卡：深色(<100)占比 0.23~0.31，p10 亮度 = 41
DIM_DARK_RATIO = 0.12
DIM_P10 = 100
ICON_MIN_PIXELS = 25   # 「同事沟通进度」人像图标的青色像素阈值（实测约 77，截断卡 68）


def card_ink(img: np.ndarray, card: Rect) -> dict:
    """量一张卡片里的"墨迹"深浅：文字越淡，深色像素越少、p10 越高。"""
    y0, y1 = max(0, card.y + 8), min(img.shape[0], card.y + card.h - 8)
    x0, x1 = max(0, card.x + 60), min(img.shape[1], card.x + 700)
    sub = img[y0:y1, x0:x1]
    if sub.size == 0:
        return {"ink": 0, "dark_ratio": 0.0, "p10": 255.0}
    lum = sub.mean(axis=2)
    ink = lum[lum < 240]
    if ink.size == 0:
        return {"ink": 0, "dark_ratio": 0.0, "p10": 255.0}
    return {
        "ink": int(ink.size),
        "dark_ratio": round(float((ink < 100).mean()), 4),
        "p10": round(float(np.percentile(ink, 10)), 1),
    }


def is_contacted(img: np.ndarray, card: Rect) -> tuple[bool, dict]:
    """判断这张卡片是否属于"文字淡化 = 同事已沟通过"，是则应当跳过、不点击。"""
    stats = card_ink(img, card)
    dim = stats["dark_ratio"] < DIM_DARK_RATIO or stats["p10"] > DIM_P10
    stats["dim"] = dim
    return dim, stats


ICON_TEMPLATE_PATH = Path(__file__).parent / "assets" / "icon_person.png"
ICON_MATCH_MIN = 0.80   # 模板匹配得分阈值（实测：有人像图标 ≈1.000，没有 ≈0.46）
_icon_template = None


def _load_icon_template():
    """人像图标模板（用 calibrate_icon.py --write 生成），只加载一次。"""
    global _icon_template
    if _icon_template is None:
        try:
            import cv2

            _icon_template = cv2.imread(str(ICON_TEMPLATE_PATH))
        except Exception:
            _icon_template = False
    return None if _icon_template is False else _icon_template


def progress_icon(img: np.ndarray, btn: Rect) -> dict:
    """检测「打招呼」按钮左侧那排图标里**是否有人像图标**（同事沟通进度）。

    这一排可能有 1~2 个图标（饼图 / 人像），且右对齐：只有饼图时它会挪到人像的位置上。
    所以不能数"青色像素"（两者都是青色、像素数相同），必须按**形状**模板匹配。
    """
    k = scale_of(btn.w)
    y0, y1 = max(0, btn.cy - int(round(24 * k))), min(img.shape[0], btn.cy + int(round(24 * k)))
    x0, x1 = max(0, btn.x - int(round(112 * k))), min(img.shape[1], btn.x - int(round(2 * k)))
    strip = img[y0:y1, x0:x1]
    if strip.size == 0:
        return {"score": 0.0, "present": False, "ambiguous": False, "pixels": 0}

    b, g, r = (strip[:, :, i].astype(int) for i in range(3))
    n = int(((g > 110) & (b > 110) & (r < 150) & ((g - r) > 20)).sum())

    tmpl = _load_icon_template()
    if tmpl is None:  # 没模板时退回旧行为：有青色图标就算
        return {"score": -1.0, "present": n >= ICON_MIN_PIXELS,
                "ambiguous": 0 < n < ICON_MIN_PIXELS, "pixels": n}

    import cv2

    if strip.shape[0] < tmpl.shape[0] or strip.shape[1] < tmpl.shape[1]:
        return {"score": 0.0, "present": False, "ambiguous": False, "pixels": n}
    res = cv2.matchTemplate(strip, tmpl, cv2.TM_CCOEFF_NORMED)
    score = float(res.max())
    return {"score": round(score, 3),
            "present": score >= ICON_MATCH_MIN,
            "ambiguous": 0.5 <= score < ICON_MATCH_MIN,
            "pixels": n}



# 「热搜牛人推荐」这类内嵌推荐块的背景是淡青色，实测 RGB ≈ (236,249,249)，
# 与普通卡片纯白 (255,255,255)、页面底色 (243,247,250) 都能分开。
SPECIAL_BG_MIN = 9      # g-r 与 b-r 的最小差值
SPECIAL_MIN_W = 600
SPECIAL_MIN_H = 80


def _is_page_bg(img: np.ndarray, x: int, y: int) -> bool:
    """页面底色 ≈ RGB(243,246,249)，用它判断"卡片/推荐块结束、回到列表背景"。

    注意 img 内部是 BGR，通道顺序别搞反。
    """
    bl, g, r = int(img[y, x, 0]), int(img[y, x, 1]), int(img[y, x, 2])
    return abs(r - 243) <= 5 and (bl - r) >= 3 and (g - r) >= 1 and r < 251


def find_special_blocks(img: np.ndarray) -> list[Rect]:
    """找出页面里内嵌的推荐块（热搜牛人推荐等）。

    这类块的顶部有一条淡青色标题带（实测 RGB ≈ 236,249,249，宽 >600、高 >15），
    块体是近白色，向下到页面底色结束。识别后整块区域都不点。
    """
    b, g, r = (img[:, :, i].astype(int) for i in range(3))
    mint = (g - r >= 9) & (b - r >= 9) & (r > 220) & (r < 252)
    h, w = img.shape[:2]
    rects: list[Rect] = []
    for y0, y1 in _runs(mint.any(axis=1), 15):
        band = mint[y0 : y1 + 1]
        wide_cols = _runs(band.any(axis=0), SPECIAL_MIN_W)
        if not wide_cols:
            continue
        x0, x1 = max(wide_cols, key=lambda c: c[1] - c[0])
        if band[:, x0 : x1 + 1].mean() < 0.5:      # 这条带里得大部分是默认色，才算块的标题带
            continue
        top = max(0, y0 - 6)
        # 这里只返回"标题带"作为锚点；块的下边界由 block_zones() 用下一张卡片的位置推断，
        # 因为块体本身是近白色，靠颜色判断下边界不可靠。
        rects.append(Rect(x0, top, x1 - x0 + 1, y1 - top + 7))
    return rects


def block_zones(strips: list[Rect], cards: list[Rect], img_h: int) -> list[Rect]:
    """把"标题带"扩展成整块禁点区域：从标题带顶部，到下一张正常卡片的上沿。"""
    zones: list[Rect] = []
    for s in strips:
        below = [c.y for c in cards if c.y > s.y + s.h]
        bottom = (min(below) - 4) if below else (s.y + 300)
        bottom = min(img_h, max(bottom, s.y + s.h))
        zones.append(Rect(s.x, s.y, s.w, bottom - s.y))
    return zones


def inside_any(rect: Rect, blocks: list[Rect], pad: int = 0) -> Rect | None:
    """卡片中心是否落在某个内嵌推荐块里（有重叠即算）。"""
    for blk in blocks:
        if (blk.x - pad <= rect.cx <= blk.x + blk.w + pad
                and blk.y - pad <= rect.cy <= blk.y + blk.h + pad):
            return blk
    return None


HOT_KEYWORDS = ("热搜", "牛人推荐", "为你推荐", "推荐理由")


def read_block_labels(img: np.ndarray, strips: list[Rect], engine=None) -> list[dict]:
    """对候选推荐块做一次小范围 OCR，取出它的标题文字（用来确认是不是「热搜牛人推荐」）。

    只 OCR 标题带那一条（约 1050x50），耗时 <0.5s，而且只在真的检测到块时才做。
    """
    engine = engine or load_ocr()
    out: list[dict] = []
    for s in strips:
        text = ""
        if engine is not None:
            region = Rect(s.x, s.y, min(560, s.w), min(s.h + 14, 70))   # 标题在块左上角
            items = ocr_crop(engine, img, region)
            text = " ".join(it["text"] for it in items)
        joined = text.replace(" ", "")
        out.append({
            "text": text,
            "is_hot": any(k in joined for k in HOT_KEYWORDS),
            "rect": s,
        })
    return out


def find_block_by_text(img: np.ndarray, cards: list[Rect], engine=None) -> list[Rect]:
    """纯文字方式找推荐块：只 OCR 列表左侧那一竖条（标题都在块左上角）。

    比颜色法慢（实测 2~3 秒/次），但不受配色变化影响；作为 `--block-text-scan` 的可选模式，
    默认关闭，只在"配色改了、颜色法失灵"时打开。
    """
    engine = engine or load_ocr()
    if engine is None or not cards:
        return []
    left = min(c.x for c in cards)
    right = left + 520                      # 标题「热搜牛人推荐」在这一段里
    region = Rect(max(0, left - 30), 0, min(right, img.shape[1]) - max(0, left - 30), img.shape[0])
    items = ocr_crop(engine, img, region)
    for it in items:
        t = it["text"].replace(" ", "")
        if "热搜" in t and "推荐" in t:
            x0, y0, x1, y1 = it["box"]
            blk_left = min(c.x for c in cards)
            blk_right = max(c.x + c.w for c in cards)
            below = [c.y for c in cards if c.y > y1]
            bottom = (min(below) - 4) if below else min(img.shape[0], y0 + 300)
            top = max(0, y0 - 10)
            return [Rect(blk_left, top, blk_right - blk_left, max(bottom - top, 80))]
    return []

def card_signature(img: np.ndarray, card: Rect) -> int:
    """卡片内容指纹：用于跨屏/刷新后识别"这个人已经处理过了"。

    做法：把卡片文字区缩成 24x12 灰度、二值化后打包成整数。
    同一张卡片重绘（滚动后回到原位、刷新后重新出现）指纹一致；
    不同候选人几乎必然不同。
    """
    y0, y1 = max(0, card.y + 6), min(img.shape[0], card.y + card.h - 6)
    x0, x1 = max(0, card.x + 60), min(img.shape[1], card.x + 760)
    sub = img[y0:y1, x0:x1]
    if sub.size == 0:
        return 0
    lum = sub.mean(axis=2)
    # 缩略：按块取均值
    th, tw = 12, 24
    hh, ww = lum.shape[0] // th, lum.shape[1] // tw
    if hh < 1 or ww < 1:
        return 0
    small = lum[: hh * th, : ww * tw].reshape(th, hh, tw, ww).mean(axis=(1, 3))
    bits = (small < 200).astype(np.uint8).ravel()
    sig = 0
    for b in bits:
        sig = (sig << 1) | int(b)
    return sig


def guess_state(btns: list[Rect], wides: list[Rect]) -> str:
    """粗判当前处于哪个界面，供后续编排用。"""
    if wides:
        return "resume_modal"
    if len(btns) >= 2:
        return "list"
    if len(btns) == 1:
        return "list_partial"
    return "unknown"


def load_ocr():
    try:
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR()
    except Exception as exc:
        print(f"[warn] OCR 不可用：{exc}", file=sys.stderr)
        return None


def ocr_crop(engine, img: np.ndarray, region: Rect | None = None) -> list[dict]:
    crop = img if region is None else img[region.y : region.y + region.h, region.x : region.x + region.w]
    if crop.size == 0:
        return []
    result, _ = engine(crop[:, :, ::-1])
    out = []
    for item in result or []:
        box, text, score = item[0], item[1], item[2]
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        ox = 0 if region is None else region.x
        oy = 0 if region is None else region.y
        out.append({"text": text, "score": round(float(score), 3),
                    "box": [int(min(xs)) + ox, int(min(ys)) + oy, int(max(xs)) + ox, int(max(ys)) + oy]})
    return out


def scan(ocr: bool, tag: str, use_window: bool) -> dict:
    win = winenv.find_window("BOSS直聘") if use_window else None
    if use_window and win is None:
        return {"error": "没找到标题含「BOSS直聘」的可见窗口"}
    origin = (0, 0)
    if win:
        _hwnd, rect, title = win
        origin = (rect[0], rect[1])
        img = winenv.grab_window(rect)
    else:
        img = winenv.grab_window((0, 0, 2560, 1440))

    boxes = detect_teal(img)
    btns = list_buttons(boxes)
    wides = wide_buttons(boxes)
    state = guess_state(btns, wides)

    engine = load_ocr() if ocr else None
    items = []
    for i, btn in enumerate(btns, 1):
        card = card_for(btn)
        texts = ocr_crop(engine, img, card) if engine else []
        items.append({"index": i, "card": asdict(card), "greet_button": asdict(btn),
                      "name_guess": texts[0]["text"] if texts else "", "texts": texts})

    OUT.mkdir(exist_ok=True)
    stem = OUT / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}"
    ann = Image.fromarray(img[:, :, ::-1])
    d = ImageDraw.Draw(ann)
    for i, it in enumerate(items, 1):
        c, b = it["card"], it["greet_button"]
        d.rectangle([c["x"], c["y"], c["x"] + c["w"], c["y"] + c["h"]], outline=(0, 90, 255), width=3)
        d.rectangle([b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]], outline=(255, 0, 0), width=3)
        d.text((c["x"] + 10, c["y"] + 8), f"#{i}", fill=(255, 0, 0))
    for w_ in wides:
        d.rectangle([w_.x, w_.y, w_.x + w_.w, w_.y + w_.h], outline=(255, 0, 255), width=3)
    ann.save(f"{stem}-annotated.png")
    Image.fromarray(img[:, :, ::-1]).save(f"{stem}.png")

    report = {
        "window": {"title": win[2], "rect": list(win[1]), "origin": origin} if win else {"screen": [img.shape[1], img.shape[0]]},
        "state": state,
        "teal_blocks": len(boxes),
        "list_buttons": len(btns),
        "wide_buttons": [asdict(b) for b in wides],
        "items": items,
        "files": {"raw": f"{stem}.png", "annotated": f"{stem}-annotated.png"},
    }
    Path(f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan"])
    ap.add_argument("--ocr", action="store_true")
    ap.add_argument("--tag", default="scan")
    ap.add_argument("--screen", action="store_true", help="不找窗口，直接抓主屏")
    args = ap.parse_args()

    winenv.enable_dpi_awareness()
    r = scan(ocr=args.ocr, tag=args.tag, use_window=not args.screen)
    if "error" in r:
        print(r["error"])
        return 2
    print(f"window: {r['window']}")
    print(f"state={r['state']}  teal={r['teal_blocks']}  list_btn={r['list_buttons']}  wide_btn={len(r['wide_buttons'])}")
    for it in r["items"]:
        c, b = it["card"], it["greet_button"]
        print(f"  #{it['index']:<2} card=({c['x']},{c['y']},{c['w']}x{c['h']}) btn=({b['x']},{b['y']}) {it['name_guess']}")
    print(f"标注图: {r['files']['annotated']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
