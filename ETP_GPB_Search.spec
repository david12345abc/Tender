# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules


a = Analysis(
    ['desktop_search.py'],
    pathex=[],
    binaries=[],
    datas=[('start_chrome.ps1', '.'), ('data', 'data')],
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
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
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
