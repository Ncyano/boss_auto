# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['boss_vision/gui.py'],
    pathex=['boss_vision'],
    binaries=[('C:/Users/Administrator/anaconda3/Library/bin/tcl86t.dll', '.'), ('C:/Users/Administrator/anaconda3/Library/bin/tk86t.dll', '.')],
    datas=[('C:/Users/Administrator/anaconda3/Library/lib/tcl8.6', 'tcl8.6'), ('C:/Users/Administrator/anaconda3/Library/lib/tcl8', 'tcl8'), ('运营助理知识库_v0.1', '运营助理知识库_v0.1'), ('boss_vision/assets', 'boss_vision/assets'), ('全国普通高等学校名单.xls', '.'), ('全国成人高等学校名单.xls', '.'), ('config.example.json', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'pandas', 'scipy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BOSS招聘助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='BOSS招聘助手',
)
