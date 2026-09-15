# PyInstaller spec for Cronus Launcher (Windows, onefile, console).
#
# Build:  pip install -r requirements.txt pyinstaller
#         pyinstaller cronus_launcher.spec
# Output: dist/CronusLauncher.exe
#
# NOTES
# - console=True keeps the console window with logs, same as Run.cmd today.
# - assets/ui/lua are bundled and resolved through app_paths.resource_path()
#   which reads sys._MEIPASS when frozen. Do not load them via plain
#   relative paths or the exe will not find them.
# - The same exe re-runs itself as the multi-roblox guard
#   (roblox_hybrid.ensure_multi_roblox_guard -> EXECUTABLE_PATH
#   --multi-roblox-guard). A onefile exe pays a full extract on every
#   guard spawn, so if guard-ready takes longer than ~3 seconds on a slow
#   disk, switch to onedir (a folder build zipped for Releases) instead.
# - version.py APP_VERSION must match the release tag (vX.Y.Z).
#   The release workflow fails the build when they differ.

import os

# Run pyinstaller from the repository root so these relative paths resolve.
block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("assets", "assets"),
        ("ui", "ui"),
        ("lua", "lua"),
    ],
    hiddenimports=[
        "PIL",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CronusLauncher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows needs .ico. Generate it before building:
    #   python -c "from PIL import Image; Image.open('assets/cronus_icon.png').save('assets/cronus_icon.ico')"
    # The release workflow does this automatically.
    icon="assets/cronus_icon.ico" if os.path.exists(os.path.join("assets", "cronus_icon.ico")) else None,
)
