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
from PyInstaller.utils.hooks import collect_data_files

# Run pyinstaller from the repository root so these relative paths resolve.
block_cipher = None


def _qtwebview2_lib_datas():
    """Mirror qtwebview2/lib/* to top-level lib/ in the bundle.

    Upstream hook (qtwebview2 0.5.0) collects lib/ under qtwebview2/lib/,
    but _dotnet_bridge.get_absolute_path() resolves sys._MEIPASS/lib/...
    in frozen builds, so the exe ships without findable .NET assemblies
    (v2.2.0 black-window crash). This maps the same tree to where the
    bridge actually looks. Subdirectory layout (runtimes/...) is preserved
    because .NET probes native deps relative to the Core assembly.
    """
    try:
        import pathlib
        import qtwebview2
    except Exception:
        return []
    root = pathlib.Path(qtwebview2.__file__).resolve().parent / "lib"
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parent = path.parent.relative_to(root)
        dest = "lib" if str(rel_parent) == "." else str(pathlib.Path("lib") / rel_parent).replace("\\", "/")
        out.append((str(path), dest))
    return out


_QTWEBVIEW2_LIB_DATAS = _qtwebview2_lib_datas()
_CERTIFI_DATAS = collect_data_files("certifi")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("assets", "assets"),
        ("ui", "ui"),
        ("lua", "lua"),
        *_QTWEBVIEW2_LIB_DATAS,
        *_CERTIFI_DATAS,
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
        # WebView2-only shell (desktop/webview_window.py): never bundle the
        # QtWebEngine Chromium stack (~100MB, 1.3GB runtime). qtwebview2
        # ships its own PyInstaller hook for lib/.
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebEngine",
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
