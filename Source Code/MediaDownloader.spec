# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

curl_datas, curl_binaries, curl_hiddenimports = collect_all('curl_cffi')
phantomjs_binary = [('tools\\phantomjs.exe', '.')]

hiddenimports = collect_submodules('yt_dlp') + curl_hiddenimports + [
    'imageio_ffmpeg',
    'PIL.Image',
]
datas = collect_data_files('imageio_ffmpeg') + curl_datas

a = Analysis(
    ['src\\media_downloader.py'],
    pathex=[],
    binaries=curl_binaries + phantomjs_binary,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='MediaDownloader',
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
