# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules


hiddenimports = [
    'openpyxl',
    'requests',
    'uvicorn',
    'fastapi',
    'scripts.concurrency',
    'scripts.dmx_client',
    'scripts.model_provider',
    'scripts.pipeline_log',
    'scripts.process_amazon',
    'scripts.process_ebay_tk',
]
for package in (
    'crosspilot',
    'web',
    'scripts',
):
    hiddenimports.extend(collect_submodules(package))


a = Analysis(
    ['main_cli.py'],
    pathex=['.'],
    binaries=[],
    datas=[('web/static', 'web/static'), ('crosspilot', 'crosspilot'), ('keys.example.json', '.'), ('使用说明.txt', '.')],
    hiddenimports=sorted(set(hiddenimports)),
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
    name='CrossPilot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
