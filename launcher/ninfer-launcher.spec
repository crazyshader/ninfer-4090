# -*- mode: python ; coding: utf-8 -*-
# ninfer-launcher PyInstaller spec (onedir, GUI)

import os

block_cipher = None

# Qt 内置简体中文翻译：PyInstaller 的 PySide6 hook 不收集 .qm 翻译文件，这里显式
# 声明 datas，把它打进 _internal/PySide6/translations/。运行时 main.
# _install_chinese_translation 经 sys._MEIPASS 找到它，确认对话框的标准按钮
# （Yes/No、OK/取消）才显示为「是 / 否 / 确定 / 取消」。构建机缺 PySide6 或
# 缺该文件时跳过（打包版标准按钮回落系统区域，其余文案不受影响）。
_datas = [('resources', 'resources')]
try:
    import PySide6 as _pyside6
    _qm_zh = os.path.join(
        os.path.dirname(os.path.abspath(_pyside6.__file__)),
        'translations',
        'qtbase_zh_CN.qm',
    )
    if os.path.isfile(_qm_zh):
        _datas.append((_qm_zh, os.path.join('PySide6', 'translations')))
except ImportError:
    pass

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # 内置预设随包分发：运行时由 config.builtin_presets_dir() 探测
    # （PyInstaller 6 onedir 会落在 _internal/resources/presets）
    datas=_datas,
    hiddenimports=['pynvml'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy'],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ninfer-launcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='ninfer-launcher',
)
