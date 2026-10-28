"""生成热更新补丁包（只含改动的源码与配置模板，约 200KB）。

用法：
    .venv\\Scripts\\python.exe tools\\build_patch.py

产物：dist\\热更新-<日期时间>.zip
"""

from __future__ import annotations

import shutil
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"

README = """# 热更新补丁

## 用法
把压缩包里的 `app` 目录**解压覆盖**到目标机包内的 `app` 目录（同名覆盖）。
覆盖前建议把 `app\\boss_vision` 复制一份改名 `boss_vision_bak` 备份。

## 本次包含的修复
1. winenv.py    抓图为 CDP 时强制走 CDP 输入（不再用真实鼠标、不受 DPI 影响）
2. winenv.py    点击光标偏移 <=40px 时按实际位置继续（不再整轮中断）
3. vision.py    图标模板改用 imdecode 读取（中文路径可正常加载）
4. driver.py    点开简历失败会重试一次；仍失败则跳过该卡片，不再判成"不合适"
5. config.example.json  补全 OCR 链路（pipeline=ocr / qwen-vl-ocr / fast）

## 注意
- 不会覆盖 `app\\config.json`（你的密钥保留）
- 但请确认 `app\\config.json` 里有以下内容（JSON 不能有尾逗号）：

```
    "pipeline": "ocr",
    "ocr_model": "qwen-vl-ocr",
    "ocr_max_tokens": 2000,
    "fast": true,
    "save_shots": "missing"
```

以及最外层：

```
  "input": { "mode": "cdp" }
```

## 验证
重启后横幅显示 `抓图模式: CDP`，
GUI 顶部显示 `识别方式: OCR 链路  qwen-vl-ocr`。
"""


LAUNCHER = (
    "@echo off\r\n"
    "chcp 65001 >nul\r\n"
    'cd /d "%~dp0"\r\n'
    "set PYTHONIOENCODING=utf-8\r\n"
    'set "PATH=%~dp0python;%~dp0python\\Library\\bin;%~dp0python\\DLLs;%PATH%"\r\n'
    'start "BOSS Collector Console" cmd /k ""%~dp0python\\python.exe" -E "%~dp0app\\boss_vision\\gui.py""\r\n'
)


def main() -> int:
    stamp = time.strftime("%m%d-%H%M")
    stage = REPO / "data" / "_patch_stage"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "app" / "boss_vision").mkdir(parents=True)
    (stage / "app" / "运营助理知识库_v0.1").mkdir(parents=True)

    count = 0
    for f in (REPO / "boss_vision").glob("*.py"):
        shutil.copy2(f, stage / "app" / "boss_vision" / f.name)
        count += 1
    for f in (REPO / "运营助理知识库_v0.1").glob("*.json"):
        shutil.copy2(f, stage / "app" / "运营助理知识库_v0.1" / f.name)
        count += 1
    src = REPO / "config.example.json"
    if src.exists():
        shutil.copy2(src, stage / "app" / "config.example.json")
        count += 1

    data_src = REPO / "data"
    if data_src.exists():
        (stage / "app" / "data").mkdir(parents=True, exist_ok=True)
        for f in data_src.glob("schools*"):
            shutil.copy2(f, stage / "app" / "data" / f.name)
            count += 1

    edge = REPO / "run-edge-debug.cmd"
    if edge.exists():
        shutil.copy2(edge, stage / "启动调试浏览器.cmd")
    rd = REPO / "使用说明.md"
    if rd.exists():
        shutil.copy2(rd, stage / "使用说明.md")

    (stage / "诊断.cmd").write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        'cd /d "%~dp0"\r\n'
        "set PYTHONIOENCODING=utf-8\r\n"
        "echo Starting in diagnostic mode (output -> diag.txt) ...\r\n"
        'set "PATH=%~dp0python;%~dp0python\\Library\\bin;%~dp0python\\DLLs;%PATH%"\r\n'
        '".\\python\\python.exe" -X faulthandler "app\\boss_vision\\gui.py" > diag.txt 2>&1\r\n'
        "echo.\r\n"
        "echo Exited. Please send diag.txt to the developer.\r\n"
        "pause\r\n",
        encoding="ascii")

    (stage / "启动控制台.cmd").write_text(LAUNCHER, encoding="ascii")

    (stage / "补丁说明.md").write_text(README, encoding="utf-8")

    zpath = DIST / f"热更新-{stamp}.zip"
    zpath.parent.mkdir(parents=True, exist_ok=True)
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in stage.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(stage))
    shutil.rmtree(stage)

    print(f"  文件数: {count + 1}")
    print(f"  补丁包: {zpath}  ({zpath.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
