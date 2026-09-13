# -*- mode: python ; coding: utf-8 -*-
# ninfer-launcher CLI PyInstaller spec（onedir，console 程序）
#
# 与 GUI spec（ninfer-launcher.spec）的三处关键差异（docs/01-ninfer-launcher-cli.md 第 9 节）：
# 1. console=True —— GUI 版 exe 没有控制台，stdout 无处可去，调用方读不到那行 JSON；
# 2. excludes 加 PySide6 —— 若 cli/ 代码不小心 import 了 ui，构建期直接报错，
#    把「零 Qt 约束」从纪律变成自动关卡；
# 3. 无 datas —— CLI 只读配置根（%LOCALAPPDATA%）里的预设，不随包分发 resources/，
#    配置根为空时明确报 no-preset，而不是悄悄用出厂预设起服务。

import os

block_cipher = None

a = Analysis(
    ['cli_main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['pynvml'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy', 'PySide6', 'shiboken6'],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ninfer-launcher-cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='ninfer-launcher-cli',
)
