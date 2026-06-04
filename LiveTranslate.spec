# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('config.yaml', '.'), ('i18n', 'i18n'), ('prompts', 'prompts'), ('assets', 'assets'), ('funasr_nano', 'funasr_nano'), ('browser-extension', 'browser-extension'), ('docs', 'docs'), ('screenshot', 'screenshot')],
    hiddenimports=['pyaudiowpatch', 'PyQt6', 'asr_engine', 'asr_sensevoice', 'asr_funasr_nano', 'asr_anime_whisper', 'asr_providers.openai_audio', 'asr_providers.assemblyai', 'asr_providers.assemblyai_streaming', 'websocket', 'home_presets', 'audio_file_translate'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
splash = Splash(
    'assets\\startup-splash.png',
    binaries=a.binaries,
    datas=a.datas,
    text_pos=None,
    text_size=12,
    minify_script=True,
    always_on_top=True,
)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    [],
    exclude_binaries=True,
    name='LiveTranslate',
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
    splash.binaries,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LiveTranslate',
)
