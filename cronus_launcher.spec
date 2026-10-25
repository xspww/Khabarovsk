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


def _playwright_datas():
    """Bundle the Playwright driver so Manual Login works in the exe.

    The popup browser drives the machine's Chrome/Edge (no download), but
    the playwright pip package (python code + node driver binary) must be
    inside the bundle. Guarded: builds without playwright installed simply
    ship no Manual Login browser support (the UI shows an install hint).
    """
    try:
        return collect_data_files("playwright")
    except Exception:
        return []


_PLAYWRIGHT_DATAS = _playwright_datas()


def _abi3t_compat_binaries():
    """Bundle python3t.dll for Python 3.15+ builds.

    abi3 wheels built for 3.15 (cryptography, ...) import ``python3t.dll``.
    CPython keeps that DLL in ``<prefix>/abi3t-compat`` instead of next to
    the interpreter, so PyInstaller's dependency scan misses it and the
    frozen app dies on ``import cryptography``. Older Pythons have no such
    directory, so this is a no-op there.
    """
    try:
        import pathlib
        import sys
    except Exception:
        return []
    for prefix in (sys.base_prefix, sys.prefix):
        path = pathlib.Path(prefix) / "abi3t-compat" / "python3t.dll"
        try:
            if path.is_file():
                return [(str(path), ".")]
        except Exception:
            continue
    return []


_ABI3T_COMPAT_BINARIES = _abi3t_compat_binaries()

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=_ABI3T_COMPAT_BINARIES,
    datas=[
        ("assets", "assets"),
        ("ui", "ui"),
        ("lua", "lua"),
        ("LICENSE", "."),
        ("THIRD_PARTY_NOTICES.txt", "."),
        *_QTWEBVIEW2_LIB_DATAS,
        *_CERTIFI_DATAS,
        *_PLAYWRIGHT_DATAS,
    ],
    hiddenimports=[
        "PIL",
        "playwright.sync_api",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
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
        # The shell imports only QtCore/QtGui/QtWidgets (verified dependency
        # closure). Everything below is pulled into the analysis anyway and
        # adds ~50 MB of DLLs the app never loads.
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQuickControls2",
        "PySide6.QtQuickTest",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtNetwork",
        "PySide6.QtNetworkAuth",
        "PySide6.QtSvg",
        "PySide6.QtSvgWidgets",
        "PySide6.QtDataVisualization",
        "PySide6.QtCharts",
        "PySide6.QtGraphs",
        "PySide6.QtGraphsWidgets",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtXml",
        "PySide6.QtPrintSupport",
        "PySide6.QtSerialPort",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtPositioning",
        "PySide6.QtLocation",
        "PySide6.QtSensors",
        "PySide6.QtStateMachine",
        "PySide6.QtWebChannel",
        "PySide6.QtWebSockets",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtUiTools",
        "PySide6.QtScxml",
        "PySide6.QtRemoteObjects",
        "PySide6.QtTextToSpeech",
        "PySide6.QtSpatialAudio",
        "PySide6.QtHttpServer",
        "PySide6.QtConcurrent",
        "PySide6.QtDBus",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Trim the collected Qt payload down to what the app actually loads.
#
# `excludes` above stops the analysis from following the unused Qt modules,
# but the PySide6 hooks still copy every Qt DLL/plugin they notice and
# opengl32sw.dll (the software OpenGL fallback). On top of the 89 MB
# Playwright node driver that turns a ~40 MB app into a 100 MB+ onefile.
# The verified import closure of QtCore/QtGui/QtWidgets.pyd (plus the
# qwindows platform, qmodernwindowsstyle style and the ico/jpeg/gif image
# plugins) is all that ever gets dlopen()-ed, so drop the rest.
_QT_KEEP_BINARIES = {
    "qt6core.dll",
    "qt6gui.dll",
    "qt6widgets.dll",
    "qtcore.pyd",
    "qtgui.pyd",
    "qtwidgets.pyd",
    "pyside6.abi3.dll",
    "shiboken6.abi3.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
}
_QT_KEEP_PLUGINS = {
    "platforms/qwindows.dll",
    "styles/qmodernwindowsstyle.dll",
    "imageformats/qico.dll",
    "imageformats/qjpeg.dll",
    "imageformats/qgif.dll",
}
# Pillow only opens PNG/ICO and converts raw BGRA captures to "L". The AVIF
# codec alone is 7.5 MB; webp/color-management/tkinter bindings are unused.
_PIL_DROP_STEMS = {"_avif", "_webp", "_imagingcms", "_imagingtk"}


def _keep_entry(dest):
    name = dest.replace("\\", "/")
    if name.startswith("PySide6/"):
        if "/plugins/" in name:
            return name.split("/plugins/", 1)[1] in _QT_KEEP_PLUGINS
        # Drops the unused Qt DLLs/.pyd modules and every translation .qm.
        return name.rsplit("/", 1)[-1].lower() in _QT_KEEP_BINARIES
    if "PIL/" in name:
        stem = name.rsplit("/", 1)[-1].split(".", 1)[0].lower()
        return stem not in _PIL_DROP_STEMS
    return True


a.binaries = [entry for entry in a.binaries if _keep_entry(entry[0])]
a.datas = [entry for entry in a.datas if _keep_entry(entry[0])]

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
