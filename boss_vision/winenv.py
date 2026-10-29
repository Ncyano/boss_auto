"""窗口层（按平台分发）。

Windows       -> winenv_win.py（user32 抓图/点击 + CDP）
macOS / Linux -> winenv_mac.py（纯 CDP，必须用后台模式）

调用方统一 `import winenv`，不需要关心平台。
"""
from __future__ import annotations

import sys as _sys

if _sys.platform == "win32":
    import winenv_win as _impl
else:
    import winenv_mac as _impl

# 公开接口：star 导入
from importlib import import_module as _im

_impl = _im(_impl.__name__)

for _name in dir(_impl):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_impl, _name)

del _sys
