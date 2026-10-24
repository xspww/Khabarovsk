from __future__ import annotations

import ctypes
import functools
import os
import signal
import sys
from collections.abc import Callable
from typing import Any, NamedTuple

from core import flog_kv
from desktop.console_icon import APP_ICON_FILE
from infra.app_paths import APP_DATA_DIR, APP_NAME, APP_ROOT_DIR, CACHE_DIR, resource_path
from version import app_display_version

# Evergreen WebView2 bootstrapper (~2MB downloader, Microsoft official).
# Installed silently on first launch when missing so every machine gets the
# real embedded window instead of the browser fallback.
WEBVIEW2_BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
_WEBVIEW2_INSTALL_TIMEOUT_SECONDS = 300.0
_WEBVIEW2_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
_WEBVIEW2_CLIENT_KEY = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_WEBVIEW2_PROMPT_FLAG = os.path.join(APP_DATA_DIR, "webview2_install_prompted")


def _read_webview2_version() -> str:
    """Return the installed WebView2 Runtime version, or '' when missing.

    Pure registry read (HKLM/HKCU, 64/32-bit views). Works frozen or not,
    never raises, cheap enough to run on every boot.
    """
    try:
        import winreg
    except Exception:
        return ""
    base = "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\" + _WEBVIEW2_CLIENT_KEY
    base_x86 = "SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\" + _WEBVIEW2_CLIENT_KEY
    roots_keys = (
        (winreg.HKEY_LOCAL_MACHINE, base),
        (winreg.HKEY_LOCAL_MACHINE, base_x86),
        (winreg.HKEY_CURRENT_USER, base),
    )
    for root, subkey in roots_keys:
        for view in (0, winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                flags = winreg.KEY_READ | view if view else winreg.KEY_READ
                with winreg.OpenKey(root, subkey, 0, flags) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
                if str(version or "").strip():
                    return str(version).strip()
            except Exception:
                continue
    return ""


def _webview2_prompted_before() -> bool:
    try:
        return os.path.exists(_WEBVIEW2_PROMPT_FLAG)
    except Exception:
        return True


def _mark_webview2_prompted() -> None:
    try:
        os.makedirs(os.path.dirname(_WEBVIEW2_PROMPT_FLAG), exist_ok=True)
        with open(_WEBVIEW2_PROMPT_FLAG, "w", encoding="utf-8") as handle:
            handle.write("1")
    except Exception:
        "intentional: ignore exception"


def _download_webview2_bootstrapper() -> str:
    """Download the Evergreen bootstrapper. Returns the exe path or ''."""
    try:
        import urllib.request
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_download_unavailable", "warning", error=str(exc))
        return ""
    try:
        target_dir = os.path.join(CACHE_DIR, "webview2_bootstrapper")
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "MicrosoftEdgeWebview2Setup.exe")
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_download_dir_failed", "warning", error=str(exc))
        return ""
    try:
        req = urllib.request.Request(
            WEBVIEW2_BOOTSTRAPPER_URL,
            headers={"User-Agent": "CronusLauncher"},
        )
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            chunks = []
            received = 0
            while True:
                piece = resp.read(256 * 1024)
                if not piece:
                    break
                received += len(piece)
                if received > _WEBVIEW2_MAX_DOWNLOAD_BYTES:
                    flog_kv("MAIN", "desktop_webview_download_too_large", "warning", received=received)
                    return ""
                chunks.append(piece)
        with open(target, "wb") as handle:
            for piece in chunks:
                handle.write(piece)
        return target
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_download_failed", "warning", error=str(exc))
        return ""


def _install_webview2_runtime(setup_path: str) -> bool:
    """Run the bootstrapper silently. Returns True on exit code 0."""
    try:
        import subprocess
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_install_unavailable", "warning", error=str(exc))
        return False
    try:
        flog_kv("MAIN", "desktop_webview_install_start")
        proc = subprocess.run(
            [setup_path, "/silent", "/install"],
            timeout=_WEBVIEW2_INSTALL_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        ok = int(getattr(proc, "returncode", 1) or 1) == 0
        flog_kv("MAIN", "desktop_webview_install_done", ok=ok)
        return ok
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_install_failed", "warning", error=str(exc))
        return False


def _ensure_webview2_runtime() -> str:
    """Return the runtime version, installing it first when missing.

    One-time Yes/No prompt, then download + silent install + recheck.
    Returns '' when the user declines, is offline, or the install fails,
    in which case the caller falls back to the system browser.
    """
    version = _read_webview2_version()
    if version:
        return version
    if _webview2_prompted_before():
        return ""
    _mark_webview2_prompted()
    try:
        choice = ctypes.windll.user32.MessageBoxW(
            None,
            "Microsoft Edge WebView2 Runtime was not found on this PC.\n\n"
            "Install it now (one-time download, needs internet)?\n"
            "Otherwise the dashboard opens in your browser instead.",
            f"{APP_NAME} - WebView2 Runtime missing",
            0x00000004 | 0x00000040,  # MB_YESNO | MB_ICONINFORMATION
        )
    except Exception:
        return ""
    if int(choice or 0) != 6:  # IDYES
        flog_kv("MAIN", "desktop_webview_install_declined")
        return ""
    setup_path = _download_webview2_bootstrapper()
    if not setup_path:
        return ""
    _install_webview2_runtime(setup_path)
    version = _read_webview2_version()
    if version:
        flog_kv("MAIN", "desktop_webview_runtime_ready", version=version)
    else:
        flog_kv("MAIN", "desktop_webview_runtime_still_missing", "warning")
    return version


def _resolve_stdlib_executors() -> None:
    """Force-resolve PEP 810 lazy stdlib executors before Qt gets involved.

    Python 3.15 declares ThreadPoolExecutor/ProcessPoolExecutor as
    `lazy from` names. Once shiboken6 (pulled in by any PySide6 import) is
    loaded, touching those names returns a dead `lazy_import` placeholder
    instead of resolving -- so `QtWebView2Widget(...)` dies in its
    constructor with `TypeError: 'lazy_import' object is not callable`
    and the app silently falls back to the system browser. Importing the
    real submodules and rebinding the names up-front avoids the trap.
    Never raises.
    """
    try:
        import concurrent.futures as _cf
        import concurrent.futures.process as _process_mod
        import concurrent.futures.thread as _thread_mod

        _cf.ThreadPoolExecutor = _thread_mod.ThreadPoolExecutor
        _cf.ProcessPoolExecutor = _process_mod.ProcessPoolExecutor
    except Exception as exc:
        flog_kv("MAIN", "desktop_executors_preresolve_failed", "warning", error=str(exc))


def _webview_data_dir() -> str:
    path = os.path.join(CACHE_DIR, "webview2")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        "intentional: ignore exception"
    return path


def _prepare_webview_env() -> None:
    # Keep Edge WebView2 profile under our cache dir instead of roaming AppData.
    # Must be set before the widget creates its CoreWebView2 environment.
    try:
        data_dir = _webview_data_dir()
        os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", data_dir)
    except Exception:
        "intentional: ignore exception"
    # Silence benign Chromium shutdown spam on stderr (e.g.
    # "Failed to unregister class Chrome_WidgetWin_0. Error = 1411"),
    # which is not our error but scares users in the console/updater
    # window every time the app closes. Read by the WebView2 loader when
    # the environment is created, so it must be set here beforehand.
    try:
        prev = str(os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or "")
        if "--disable-logging" not in prev:
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (prev + " --disable-logging").strip()
    except Exception:
        "intentional: ignore exception"


def _set_app_user_model_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Cronus.Launcher.Desktop")
    except Exception:
        "intentional: ignore exception"


def _bundled_kanit_candidates() -> list:
    candidates: list = []
    for name in ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-Bold.ttf", "Kanit-Regular.ttf", "Kanit-Medium.ttf"):
        for base in (
            resource_path("assets", "fonts", name),
            os.path.join(APP_ROOT_DIR, "assets", "fonts", name),
        ):
            if base and base not in candidates:
                candidates.append(base)
    return candidates


def _kanit_fonts_ready(qfontdb: Any) -> bool:
    try:
        families = qfontdb.families()
    except Exception:
        return False
    return "Inter" in families and "Kanit" in families


def _load_font_candidate(qfontdb: Any, path: str) -> bool:
    try:
        if not path or not os.path.exists(path):
            return False
        font_id = qfontdb.addApplicationFont(path)
        return int(font_id) >= 0
    except Exception as exc:
        flog_kv("MAIN", "desktop_font_load_failed", "warning", path=path, error=str(exc))
        return False


def _load_bundled_kanit_fonts() -> bool:
    try:
        from PySide6.QtGui import QFontDatabase
    except Exception as exc:
        flog_kv("MAIN", "desktop_font_qt_unavailable", "debug", error=str(exc))
        return False
    if _kanit_fonts_ready(QFontDatabase):
        return True
    loaded_any = False
    for path in _bundled_kanit_candidates():
        if _load_font_candidate(QFontDatabase, path):
            loaded_any = True
    try:
        if "Inter" not in QFontDatabase.families():
            return False
    except Exception:
        return False
    if loaded_any:
        from core import flog

        flog("[MAIN] Bundled Inter/Kanit fonts loaded for Qt widgets")
    return True


def _webview2_preflight() -> bool:
    """Synchronously force the .NET/WebView2 load before creating any widget.

    QtWebView2Widget initializes async (QTimer.singleShot -> _init_webview),
    so a broken frozen layout (missing lib/*.dll) only surfaces as a dead
    black window + thread tracebacks. Preflight turns that into a clean
    False here, and the caller falls back to the system browser instead.
    """
    try:
        from qtwebview2._dotnet_bridge import load_dotnet_env
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_preflight_unavailable", "warning", error=str(exc))
        return False
    try:
        load_dotnet_env()
        return True
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_preflight_failed", "warning", error=str(exc))
        return False


_SHELL_QSS_NORMAL = (
    "QWidget#CronusWindowShell {"
    " background: #0b0c10;"
    " border: 1px solid #20232c;"
    " border-radius: 0px;"
    "}"
    "#CronusWebView { background: #0b0c10; border: 0; }"
)
_SHELL_QSS_MAXIMIZED = (
    "QWidget#CronusWindowShell {"
    " background: #0b0c10;"
    " border: 0px;"
    " border-radius: 0px;"
    "}"
    "#CronusWebView { background: #0b0c10; border: 0; }"
)

_TITLE_BAR_QSS = """
                    #CronusTitleBar {
                        background-color: #0c0d13;
                        border-bottom: 1px solid #20232c;
                    }
                    #CronusTitle {
                        font-family: "Inter", "Kanit", "Segoe UI", "Leelawadee UI", Tahoma, "Noto Sans Thai", sans-serif;
                        color: #9aa0ab;
                        font-size: 12px;
                        font-weight: 500;
                    }
                    #CronusStatusIcon {
                        margin-right: 0px;
                    }
                    QPushButton#WinMinButton, QPushButton#WinMaxButton, QPushButton#WinCloseButton {
                        width: 34px; height: 22px; min-width: 34px; max-width: 34px;
                        min-height: 22px; max-height: 22px; border-radius: 9px;
border: 1px solid #262a33;
                         background-color: #14161c;
                         color: #53565e;
                        padding: 0px;
                    }
                    QPushButton#WinMinButton:hover, QPushButton#WinMaxButton:hover {
background-color: #1a1d29;
                         border-color: #3a3f4d;
                        color: #ffffff;
                    }
                    QPushButton#WinCloseButton:hover {
background-color: #26161b;
                         border-color: #4c1d24;
                         color: #f87171;
                    }
                    """


class _QtBindings(NamedTuple):
    QCoreApplication: Any
    QPoint: Any
    QSize: Any
    Qt: Any
    QTimer: Any
    QEvent: Any
    QColor: Any
    QFont: Any
    QIcon: Any
    QPainter: Any
    QPen: Any
    QPixmap: Any
    QApplication: Any
    QFrame: Any
    QHBoxLayout: Any
    QLabel: Any
    QMainWindow: Any
    QPushButton: Any
    QVBoxLayout: Any
    QWidget: Any
    QtWebView2Widget: Any


def _load_qt_bindings() -> _QtBindings | None:
    try:
        from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QSize, Qt, QTimer
        from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
        from PySide6.QtWidgets import (
            QApplication,
            QFrame,
            QHBoxLayout,
            QLabel,
            QMainWindow,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )
        from qtwebview2 import QtWebView2Widget
    except Exception as exc:
        flog_kv("MAIN", "desktop_qt_unavailable", "warning", error=str(exc))
        return None
    return _QtBindings(
        QCoreApplication,
        QPoint,
        QSize,
        Qt,
        QTimer,
        QEvent,
        QColor,
        QFont,
        QIcon,
        QPainter,
        QPen,
        QPixmap,
        QApplication,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QPushButton,
        QVBoxLayout,
        QWidget,
        QtWebView2Widget,
    )


def _title_icon_pixmap(qt: _QtBindings, title_icon_file: str) -> Any:
    source = qt.QPixmap(title_icon_file)
    if source.isNull():
        return qt.QPixmap()
    return source.scaled(
        22,
        22,
        qt.Qt.AspectRatioMode.KeepAspectRatio,
        qt.Qt.TransformationMode.SmoothTransformation,
    )


def _window_control_icon(qt: _QtBindings, kind: str) -> Any:
    pixmap = qt.QPixmap(16, 16)
    pixmap.fill(qt.QColor(0, 0, 0, 0))
    painter = qt.QPainter(pixmap)
    painter.setRenderHint(qt.QPainter.RenderHint.Antialiasing, True)
    pen = qt.QPen(qt.QColor("#6b7a91"), 1.6)
    pen.setCapStyle(qt.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(qt.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if kind == "minimize":
        painter.drawLine(5, 8, 11, 8)
    elif kind == "maximize":
        painter.drawRect(5, 5, 6, 6)
    else:
        painter.drawLine(5, 5, 11, 11)
        painter.drawLine(11, 5, 5, 11)
    painter.end()
    return qt.QIcon(pixmap)


def _title_bar_init(bar: Any, parent: Any, title: str = APP_NAME) -> None:
    qt = bar._qt
    bar._window = parent
    bar._drag_pos = qt.QPoint()
    bar._running = False
    bar._idle_icon = _title_icon_pixmap(qt, bar._title_icon_file)
    bar._active_icon = bar._idle_icon
    bar.setObjectName("CronusTitleBar")
    bar.setFixedHeight(32)
    bar._title_label = qt.QLabel(bar)
    bar._title_label.setObjectName("CronusTitle")
    bar._title_label.setTextFormat(qt.Qt.TextFormat.RichText)
    # NOTE: QLabel with RichText renders via QTextDocument, which
    # ignores the QSS font-family and uses the widget/QApp font
    # instead. So the font must be set in BOTH places:
    # QFont (for the document default) + inline HTML style.
    try:
        title_font = qt.QFont("Inter", 9)
        title_font.setWeight(qt.QFont.Weight.Medium)
        bar._title_label.setFont(title_font)
    except Exception:
        "intentional: ignore exception"
    title_font_stack = "'Inter','Kanit','Segoe UI','Leelawadee UI',Tahoma,'Noto Sans Thai',sans-serif"
    bar._title_label.setText(
        f'<span style="font-family: {title_font_stack}; font-weight: 500;">{APP_NAME}</span> '
        f'<span style="font-family: {title_font_stack}; font-weight: 500; color: #5c616b;">- {app_display_version()}</span>'
    )
    bar._title_label.setAlignment(qt.Qt.AlignmentFlag.AlignCenter)
    bar._title_label.setAttribute(qt.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    layout = qt.QHBoxLayout(bar)
    layout.setContentsMargins(12, 0, 10, 0)
    layout.setSpacing(6)
    bar._status_icon = qt.QLabel(bar)
    bar._status_icon.setObjectName("CronusStatusIcon")
    bar._status_icon.setFixedSize(22, 22)
    bar._status_icon.setAlignment(qt.Qt.AlignmentFlag.AlignCenter)
    if not bar._idle_icon.isNull():
        bar._status_icon.setPixmap(bar._idle_icon)
    layout.addWidget(bar._status_icon)
    layout.addStretch(1)
    min_btn = bar._button("WinMinButton", "Minimize", "minimize")
    max_btn = bar._button("WinMaxButton", "Maximize", "maximize")
    close_btn = bar._button("WinCloseButton", "Close", "close")
    min_btn.clicked.connect(parent.showMinimized)
    max_btn.clicked.connect(bar._toggle_maximized)
    close_btn.clicked.connect(parent.close)
    layout.addWidget(min_btn)
    layout.addWidget(max_btn)
    layout.addWidget(close_btn)
    bar.setStyleSheet(_TITLE_BAR_QSS)


def _title_bar_resized(bar: Any, event: Any) -> None:
    bar._title_label.setGeometry(0, 0, bar.width(), bar.height())


def _title_bar_set_running(bar: Any, running: bool) -> None:
    running = bool(running)
    if running == bar._running:
        return
    bar._running = running
    icon = bar._active_icon if running else bar._idle_icon
    if not icon.isNull():
        bar._status_icon.setPixmap(icon)


def _title_bar_button(bar: Any, name: str, tooltip: str, icon_name: str) -> Any:
    button = bar._qt.QPushButton("", bar)
    button.setObjectName(name)
    button.setToolTip(tooltip)
    button.setFixedSize(34, 22)
    button.setIcon(_window_control_icon(bar._qt, icon_name))
    button.setIconSize(bar._qt.QSize(16, 16))
    button.setCursor(bar._qt.Qt.CursorShape.PointingHandCursor)
    button.setFocusPolicy(bar._qt.Qt.FocusPolicy.NoFocus)
    return button


def _title_bar_event_pos(bar: Any, event: Any) -> Any:
    try:
        return event.globalPosition().toPoint()
    except AttributeError:
        return event.globalPos()


def _title_bar_toggle_maximized(bar: Any) -> None:
    if bar._window.isMaximized():
        bar._window.showNormal()
    else:
        bar._window.showMaximized()


def _title_bar_mouse_press(bar: Any, event: Any) -> None:
    if event.button() == bar._qt.Qt.MouseButton.LeftButton:
        bar._drag_pos = bar._event_pos(event) - bar._window.frameGeometry().topLeft()
        event.accept()


def _title_bar_mouse_move(bar: Any, event: Any) -> None:
    if (
        event.buttons() & bar._qt.Qt.MouseButton.LeftButton
        and not bar._window.isMaximized()
    ):
        bar._window.move(bar._event_pos(event) - bar._drag_pos)
        event.accept()


def _title_bar_mouse_double_click(bar: Any, event: Any) -> None:
    if event.button() == bar._qt.Qt.MouseButton.LeftButton:
        bar._toggle_maximized()
        event.accept()


def _make_rounded_window_class(qt: _QtBindings) -> type:
    QMainWindow = qt.QMainWindow

    class RoundedMainWindow(QMainWindow):
        # NOTE: no QBitmap mask here. WebView2 is a native HWND child and
        # cannot be clipped by a Qt mask (airspace). Window is square
        # (no rounding): DWM DONOTROUND + Qt radius 0. The visible 1px
        # border is drawn by the Qt shell only. The web `.app` stays flat
        # (border 0 / radius 0) so there is exactly one outer edge.
        # This also removes a full-mask repaint on every resize (CPU win).
        def changeEvent(self, event):
            try:
                QEvent = qt.QEvent
            except Exception:
                QEvent = None
            try:
                if QEvent is not None and event.type() == QEvent.Type.WindowStateChange:
                    sync = getattr(self, "_cronus_sync_chrome", None)
                    if callable(sync):
                        sync(bool(self.isMaximized()))
            except Exception:
                "intentional: ignore exception"
            super().changeEvent(event)

    return RoundedMainWindow


def _make_title_bar_class(qt: _QtBindings, title_icon_file: str) -> type:
    QFrame = qt.QFrame

    class TitleBar(QFrame):
        _qt = qt
        _title_icon_file = title_icon_file

        def __init__(self, parent, title: str = APP_NAME):
            super().__init__(parent)
            _title_bar_init(self, parent, title)

        def resizeEvent(self, event):
            super().resizeEvent(event)
            _title_bar_resized(self, event)

        set_running = _title_bar_set_running
        _button = _title_bar_button
        _event_pos = _title_bar_event_pos
        _toggle_maximized = _title_bar_toggle_maximized
        mousePressEvent = _title_bar_mouse_press
        mouseMoveEvent = _title_bar_mouse_move
        mouseDoubleClickEvent = _title_bar_mouse_double_click

    return TitleBar


def _apply_windows_rounded_corners(qwindow: Any, maximized: bool = False) -> None:
    if os.name != "nt":
        return
    try:
        hwnd = int(qwindow.winId())
        # Square window: DONOTROUND (1) in both states. Maximized
        # frameless windows must not round either, otherwise DWM
        # leaves a gap / cut border at the screen edges.
        _ = maximized
        preference = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(33),
            ctypes.byref(preference),
            ctypes.sizeof(preference),
        )
    except Exception as exc:
        flog_kv("MAIN", "desktop_rounded_corner_unavailable", "debug", error=str(exc))


def _sync_chrome(window: Any, container: Any, view: Any, maximized: bool) -> None:
    try:
        container.setStyleSheet(_SHELL_QSS_MAXIMIZED if maximized else _SHELL_QSS_NORMAL)
    except Exception:
        "intentional: ignore exception"
    _apply_windows_rounded_corners(window, maximized=maximized)
    # WebView2 JS bridge: only evaluate_js exists (ExecuteScriptAsync
    # queued pre-init). Guard is_ready so a pre-init call is skipped
    # instead of raising inside the bridge.
    try:
        if bool(getattr(view, "is_ready", False)):
            view.evaluate_js(
                "try{document.documentElement.classList.toggle"
                "('cronus-maximized', %s)}catch(e){}"
                % ("true" if maximized else "false")
            )
    except Exception:
        "intentional: ignore exception"


def _build_qt_app(qt: _QtBindings) -> tuple[Any, Any, str]:
    _set_app_user_model_id()
    try:
        qt.QCoreApplication.setApplicationName(APP_NAME)
        qt.QCoreApplication.setOrganizationName("Cronus")
    except Exception:
        "intentional: ignore exception"
    app_qt = qt.QApplication.instance() or qt.QApplication(sys.argv[:1])
    kanit_ok = _load_bundled_kanit_fonts()
    try:
        if kanit_ok:
            app_qt.setFont(qt.QFont("Inter", 9))
    except Exception as exc:
        flog_kv("MAIN", "desktop_font_apply_failed", "debug", error=str(exc))
    icon_path = resource_path("assets", APP_ICON_FILE)
    icon = qt.QIcon(icon_path) if os.path.exists(icon_path) else qt.QIcon()
    if not icon.isNull():
        app_qt.setWindowIcon(icon)
    return app_qt, icon, icon_path


def _build_main_window(
    qt: _QtBindings,
    url: str,
    icon: Any,
) -> tuple[Any, Any, Any]:
    window = _make_rounded_window_class(qt)()
    window.setWindowTitle(APP_NAME)
    window.setWindowFlags(qt.Qt.WindowType.Window | qt.Qt.WindowType.FramelessWindowHint)
    # Opaque shell: WebView2 is a native HWND child and cannot be
    # clipped by a translucent Qt mask (airspace). Translucency caused
    # the buggy fringe/black corners. Outer rounding comes from DWM only.
    window.setAttribute(qt.Qt.WidgetAttribute.WA_TranslucentBackground, False)
    window.setStyleSheet("QMainWindow { background: #0b0c10; }")
    if not icon.isNull():
        window.setWindowIcon(icon)
    # Opaque WebView2 view: no translucency, no alpha compositing.
    # This is the main RAM/CPU win vs the old transparent QWebEngineView.
    view = qt.QtWebView2Widget(parent=window, url=str(url or ""))
    view.setObjectName("CronusWebView")
    view.setStyleSheet("#CronusWebView { background: #0b0c10; border: 0; }")
    container = qt.QWidget(window)
    container.setObjectName("CronusWindowShell")
    container.setAttribute(qt.Qt.WidgetAttribute.WA_StyledBackground, True)
    # Single outer edge: Qt draws the 1px border, square corners.
    # The web `.app` is flat (no border / radius / inset ring).
    container.setStyleSheet(_SHELL_QSS_NORMAL)
    return window, view, container


def _install_shell_layout(
    qt: _QtBindings,
    window: Any,
    container: Any,
    view: Any,
    title_bar: Any,
) -> None:
    # NOTE: children are inset by 1px so they never paint over the outer
    # ring. QSS border-radius does not clip child widgets, so without
    # this the square titlebar/web corners cover the rounded border.
    layout = qt.QVBoxLayout(container)
    layout.setContentsMargins(1, 1, 1, 1)
    layout.setSpacing(0)
    layout.addWidget(title_bar)
    layout.addWidget(view, 1)
    window.setCentralWidget(container)


def _show_window(
    qt: _QtBindings,
    window: Any,
    app_qt: Any,
    on_first_show: Callable[[], None] | None,
) -> None:
    # Initial size matched to the Storm Launcher reference screenshot
    # (compact ~4:3 login window, not wide 1280x820).
    window.resize(1124, 843)
    window.setMinimumSize(966, 659)
    try:
        screen = app_qt.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            window.move(geo.center() - window.rect().center())
    except Exception:
        "intentional: ignore exception"
    _apply_windows_rounded_corners(window)
    window.show()
    window.raise_()
    window.activateWindow()
    try:
        if callable(on_first_show):
            on_first_show()
    except Exception:
        "intentional: ignore exception"
    _apply_windows_rounded_corners(window)


class _DesktopRuntime:
    def __init__(
        self,
        farm: Any,
        shutdown_event: Any,
        title_timer: Any,
        app_qt: Any,
    ) -> None:
        self.farm = farm
        self.shutdown_event = shutdown_event
        self.title_timer = title_timer
        self.app_qt = app_qt
        self.shutting_down = False

    def stop(self, reason: str = "desktop_shutdown") -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        try:
            self.shutdown_event.set()
        except Exception:
            "intentional: ignore exception"
        try:
            self.title_timer.stop()
        except Exception:
            "intentional: ignore exception"
        # Fast close: window is already gone, never block on the full
        # graceful stop() (sequential 2s joins = 10s+ hang). stop_fast()
        # signals workers, kills Roblox, persists runtime, releases the
        # guard — daemon threads die with the process exit in run_desktop.
        try:
            if self.farm is not None and bool(getattr(self.farm, "running", False)):
                fast_stop = getattr(self.farm, "stop_fast", None)
                if callable(fast_stop):
                    fast_stop()
                else:
                    self.farm.stop()
        except Exception as exc:
            flog_kv("MAIN", "desktop_shutdown_stop_farm_failed", "warning", reason=reason, error=str(exc))
        try:
            from desktop.instance_guard import _clear_instance_state

            _clear_instance_state()
        except Exception:
            "intentional: ignore exception"


def _refresh_title_status(title_bar: Any, runtime: _DesktopRuntime) -> None:
    if runtime.shutting_down:
        return
    try:
        running = bool(getattr(runtime.farm, "running", False))
    except KeyboardInterrupt:
        runtime.stop("ctrl_c")
        try:
            runtime.app_qt.quit()
        except Exception:
            "intentional: ignore exception"
        return
    except Exception:
        running = False
    title_bar.set_running(running)


def _log_ctrl_c() -> None:
    from core import flog as _flog

    _flog("[MAIN] Ctrl+C received - closing desktop window")


def _handle_sigint(_signum: Any, _frame: Any, runtime: _DesktopRuntime) -> None:
    _log_ctrl_c()
    runtime.stop("ctrl_c")
    runtime.app_qt.quit()


class DesktopWindow:
    """Deep module: owns the desktop window behind a small interface.

    Interface:
      run(url, farm, shutdown_event, on_first_show) -> bool

    Hides: QApplication, WebView2 widget, TitleBar, DWM rounding,
    fonts, icons, timers, SIGINT. No QWebEngine anywhere.
    """

    def run(
        self,
        url: str,
        farm: Any,
        shutdown_event: Any,
        on_first_show: Callable[[], None] | None = None,
    ) -> bool:
        # Must run before any widget construction: see _resolve_stdlib_executors.
        _resolve_stdlib_executors()
        runtime_version = _ensure_webview2_runtime()
        if not runtime_version:
            # Clean machine where the user declined / is offline / install
            # failed: bail out early so the caller falls back to the system
            # browser instead of showing a dead black window.
            flog_kv("MAIN", "desktop_webview_runtime_missing", "warning")
            return False
        _prepare_webview_env()
        if not _webview2_preflight():
            return False
        qt = _load_qt_bindings()
        if qt is None:
            return False

        app_qt, icon, title_icon_file = _build_qt_app(qt)
        window, view, container = _build_main_window(qt, url, icon)
        title_bar = _make_title_bar_class(qt, title_icon_file)(window)
        _install_shell_layout(qt, window, container, view, title_bar)
        sync_chrome = functools.partial(_sync_chrome, window, container, view)
        window._cronus_sync_chrome = sync_chrome
        sync_chrome(bool(window.isMaximized()))
        _show_window(qt, window, app_qt, on_first_show)

        title_timer = qt.QTimer(window)
        desktop_runtime = _DesktopRuntime(farm, shutdown_event, title_timer, app_qt)
        title_timer.timeout.connect(
            functools.partial(_refresh_title_status, title_bar, desktop_runtime)
        )
        title_timer.start(500)
        window._cronus_title_timer = title_timer
        _refresh_title_status(title_bar, desktop_runtime)
        from core import flog

        flog("[MAIN] Desktop window running (WebView2)")
        previous_sigint = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(
                signal.SIGINT,
                functools.partial(_handle_sigint, runtime=desktop_runtime),
            )
        except Exception:
            previous_sigint = None
        try:
            app_qt.exec()
        except KeyboardInterrupt:
            _log_ctrl_c()
            desktop_runtime.stop("ctrl_c")
        finally:
            if previous_sigint is not None:
                try:
                    signal.signal(signal.SIGINT, previous_sigint)
                except Exception:
                    "intentional: ignore exception"
            desktop_runtime.stop("desktop_window_closed")
        return True
