# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


datas = [('start_chrome.ps1', '.'), ('data', 'data')]
if Path('tools').exists():
    datas.append(('tools', 'tools'))
if Path('temp').exists():
    datas.append(('temp', 'temp'))
elif Path('../temp').exists():
    datas.append(('../temp', 'temp'))

extra_datas: list = []
extra_binaries: list = []
extra_hiddenimports: list = []

for pkg in ('sentence_transformers', 'transformers', 'torch', 'tiktoken', 'pymorphy3', 'pymorphy3_dicts_ru'):
    try:
        d, b, h = collect_all(pkg)
        extra_datas += d
        extra_binaries += b
        extra_hiddenimports += h
    except Exception:
        pass

try:
    d, b, h = collect_all('faiss_cpu')
    extra_datas += d
    extra_binaries += b
    extra_hiddenimports += h
except Exception:
    try:
        d, b, h = collect_all('faiss')
        extra_datas += d
        extra_binaries += b
        extra_hiddenimports += h
    except Exception:
        pass

try:
    d, b, h = collect_all('pymupdf')
    extra_datas += d
    extra_binaries += b
    extra_hiddenimports += h
except Exception:
    pass

a = Analysis(
    ['desktop_search.py'],
    pathex=[],
    binaries=extra_binaries,
    datas=datas + extra_datas,
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
        *collect_submodules('desktop_app'),
        'fitz',
        'PIL',
        'PIL.Image',
        'faiss',
        'numpy',
        'pythoncom',
        'pywintypes',
        'win32com.client',
        *extra_hiddenimports,
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
    upx=False,
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
    upx=False,
    upx_exclude=[],
    name='ETP_GPB_Search',
)
