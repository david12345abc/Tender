# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


datas = [('start_chrome.ps1', '.'), ('data', 'data')]
if Path('tools').exists():
    datas.append(('tools', 'tools'))


a = Analysis(
    ['desktop_search.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'selenium.webdriver.chrome.webdriver',
        'selenium.webdriver.edge.webdriver',
        'selenium.webdriver.common.driver_finder',
        'selenium.webdriver.common.selenium_manager',
        *collect_submodules('webdriver_manager'),
        *collect_submodules('docx'),
        *collect_submodules('pypdf'),
        *collect_submodules('py7zr'),
        *collect_submodules('rarfile'),
        *collect_submodules('pptx'),
        *collect_submodules('odf'),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ETP_GPB_Search',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    name='ETP_GPB_Search',
)
