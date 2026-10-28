"""可维护的 OCR 错字修正表。

OCR 偶尔会把字认错或重复（例如「暑托班」被识别成「暑署托班」）。这类错误没有通用规则，
所以做成一张**可以随时手工增删的对照表**：`boss_vision/corrections.json`

    {
      "暑署托班": "暑托班",
      "有限公司Q": "有限公司"
    }

写入 results.json / 生成 CSV / 回填历史 时都会自动套用（长串优先替换，避免互相覆盖）。
"""
from __future__ import annotations

import json
from pathlib import Path

_HERE = Path(__file__).parent
CORRECTIONS_PATH = _HERE / "corrections.json"

DEFAULT_CORRECTIONS = {
    "暑署托班": "暑托班",
}

_cache: list[tuple[str, str]] | None = None


def load_corrections(refresh: bool = False) -> list[tuple[str, str]]:
    """读对照表，按"左边越长越优先"排序，避免短串先把长串拆坏。"""
    global _cache
    if _cache is not None and not refresh:
        return _cache

    table: dict[str, str] = dict(DEFAULT_CORRECTIONS)
    if CORRECTIONS_PATH.exists():
        try:
            raw = json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                table.update({str(k): str(v) for k, v in raw.items()})
        except Exception as exc:
            print(f"[warn] corrections.json 读取失败：{exc}")

    _cache = sorted(table.items(), key=lambda kv: len(kv[0]), reverse=True)
    return _cache


def fix_text(text: str) -> str:
    """套用错字表。"""
    if not text:
        return text
    for wrong, right in load_corrections():
        if wrong and wrong in text:
            text = text.replace(wrong, right)
    return text


def fix_obj(obj):
    """递归修正 dict / list / str 里的所有字符串（不改动数字等其它类型）。"""
    if isinstance(obj, str):
        return fix_text(obj)
    if isinstance(obj, dict):
        return {k: fix_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fix_obj(v) for v in obj]
    return obj


def ensure_file() -> Path:
    """确保对照表存在（第一次运行时写一份带示例的文件，方便直接编辑）。"""
    if not CORRECTIONS_PATH.exists():
        CORRECTIONS_PATH.write_text(
            json.dumps(DEFAULT_CORRECTIONS, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return CORRECTIONS_PATH
