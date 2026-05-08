# -*- mode: python ; coding: utf-8 -*-
# Одна exe: при старте распаковка во временный каталог на локальном диске (%TEMP%).
# Так обычно обходится ошибка LoadLibrary для python313.dll при запуске с UNC \\server\share...
#
# Сборка: python -m PyInstaller --clean --noconfirm ETP_GPB_Search_onefile.spec
# Результат: dist\ETP_GPB_Search.exe (очень большой файл; первый запуск дольше из‑за распаковки).

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


datas = [('start_chrome.ps1', '.'), ('data', 'data')]
if Path('tools').exists():
    datas.append(('tools', 'tools'))

extra_datas: list = []
extra_binaries: list = []
extra_hiddenimports: list = []

for pkg in ('sentence_transformers', 'transformers', 'torch', 'tiktoken'):
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ETP_GPB_Search',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
