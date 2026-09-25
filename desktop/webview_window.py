from __future__ import annotations

import ctypes
import os
import signal
import sys
from typing import Any, Callable, Optional

from app_paths import APP_DATA_DIR, APP_NAME, APP_ROOT_DIR, CACHE_DIR, resource_path
from version import app_display_version
from core import flog_kv
from desktop.console_icon import APP_ICON_FILE

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
        pass


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


def _webview_data_dir() -> str:
    path = os.path.join(CACHE_DIR, "webview2")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def _prepare_webview_env() -> None:
    # Keep Edge WebView2 profile under our cache dir instead of roaming AppData.
    # Must be set before the widget creates its CoreWebView2 environment.
    try:
        data_dir = _webview_data_dir()
        os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", data_dir)
    except Exception:
        pass
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
        pass


def _set_app_user_model_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Cronus.Launcher.Desktop")
    except Exception:
        pass


def _bundled_kanit_candidates() -> list:
    candidates: list = []
    for name in ("Kanit-Regular.ttf", "Kanit-Medium.ttf"):
        for base in (
            resource_path("assets", "fonts", name),
            os.path.join(APP_ROOT_DIR, "assets", "fonts", name),
        ):
            if base and base not in candidates:
                candidates.append(base)
    return candidates


def _load_bundled_kanit_fonts() -> bool:
    try:
        from PySide6.QtGui import QFontDatabase
    except Exception as exc:
        flog_kv("MAIN", "desktop_font_qt_unavailable", "debug", error=str(exc))
        return False
    try:
        if "Kanit" in QFontDatabase.families():
            return True
    except Exception:
        pass
    loaded_any = False
    for path in _bundled_kanit_candidates():
        try:
            if not path or not os.path.exists(path):
                continue
            font_id = QFontDatabase.addApplicationFont(path)
            if int(font_id) >= 0:
                loaded_any = True
        except Exception as exc:
            flog_kv("MAIN", "desktop_font_load_failed", "warning", path=path, error=str(exc))
    try:
        if "Kanit" in QFontDatabase.families():
            if loaded_any:
                from core import flog

                flog("[MAIN] Bundled Kanit fonts loaded for Qt widgets")
            return True
    except Exception:
        pass
    return False


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
        on_first_show: Optional[Callable[[], None]] = None,
    ) -> bool:
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
        try:
            from PySide6.QtCore import QPoint, QSize, QTimer, Qt, QCoreApplication
            from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
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
            return False

        TITLE_ICON_FILE = resource_path("assets", APP_ICON_FILE)

        def _title_icon_pixmap() -> QPixmap:
            source = QPixmap(TITLE_ICON_FILE)
            if source.isNull():
                return QPixmap()
            return source.scaled(
                22,
                22,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        def _window_control_icon(kind: str) -> QIcon:
            pixmap = QPixmap(16, 16)
            pixmap.fill(QColor(0, 0, 0, 0))
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(QColor("#6b7a91"), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            if kind == "minimize":
                painter.drawLine(5, 8, 11, 8)
            elif kind == "maximize":
                painter.drawRect(5, 5, 6, 6)
            else:
                painter.drawLine(5, 5, 11, 11)
                painter.drawLine(11, 5, 5, 11)
            painter.end()
            return QIcon(pixmap)

        class RoundedMainWindow(QMainWindow):
            # NOTE: no QBitmap mask here. WebView2 is a native HWND child and
            # cannot be clipped by a Qt mask (airspace). Rounding comes from
            # DWM (DwmSetWindowAttribute 33) + CSS radius. This also removes
            # a full-mask repaint on every resize (CPU win).
            pass

        class TitleBar(QFrame):
            def __init__(self, parent, title: str = APP_NAME):
                super().__init__(parent)
                self._window = parent
                self._drag_pos = QPoint()
                self._running = False
                self._idle_icon = _title_icon_pixmap()
                self._active_icon = self._idle_icon
                self.setObjectName("CronusTitleBar")
                self.setFixedHeight(32)
                self._title_label = QLabel(self)
                self._title_label.setObjectName("CronusTitle")
                self._title_label.setTextFormat(Qt.TextFormat.RichText)
                self._title_label.setText(f'<span>{APP_NAME}</span> <span style="color: #5c616b;">- {app_display_version()}</span>')
                self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                layout = QHBoxLayout(self)
                layout.setContentsMargins(12, 0, 10, 0)
                layout.setSpacing(6)
                self._status_icon = QLabel(self)
                self._status_icon.setObjectName("CronusStatusIcon")
                self._status_icon.setFixedSize(22, 22)
                self._status_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
                if not self._idle_icon.isNull():
                    self._status_icon.setPixmap(self._idle_icon)
                layout.addWidget(self._status_icon)
                layout.addStretch(1)
                min_btn = self._button("WinMinButton", "Minimize", "minimize")
                max_btn = self._button("WinMaxButton", "Maximize", "maximize")
                close_btn = self._button("WinCloseButton", "Close", "close")
                min_btn.clicked.connect(parent.showMinimized)
                max_btn.clicked.connect(self._toggle_maximized)
                close_btn.clicked.connect(parent.close)
                layout.addWidget(min_btn)
                layout.addWidget(max_btn)
                layout.addWidget(close_btn)
                self.setStyleSheet(
                    """
                    #CronusTitleBar {
                        background-color: #0c0d13;
                        border-bottom: 1px solid #20232c;
                        border-top-left-radius: 10px;
                        border-top-right-radius: 10px;
                    }
                    #CronusTitle {
                        font-family: "Kanit", "Segoe UI", "Leelawadee UI", Tahoma, "Noto Sans Thai", sans-serif;
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
                )

            def resizeEvent(self, event):
                super().resizeEvent(event)
                self._title_label.setGeometry(0, 0, self.width(), self.height())

            def set_running(self, running: bool):
                running = bool(running)
                if running == self._running:
                    return
                self._running = running
                icon = self._active_icon if running else self._idle_icon
                if not icon.isNull():
                    self._status_icon.setPixmap(icon)

            def _button(self, name: str, tooltip: str, icon_name: str):
                button = QPushButton("", self)
                button.setObjectName(name)
                button.setToolTip(tooltip)
                button.setFixedSize(34, 22)
                button.setIcon(_window_control_icon(icon_name))
                button.setIconSize(QSize(16, 16))
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                return button

            def _event_pos(self, event):
                try:
                    return event.globalPosition().toPoint()
                except AttributeError:
                    return event.globalPos()

            def _toggle_maximized(self):
                if self._window.isMaximized():
                    self._window.showNormal()
                else:
                    self._window.showMaximized()

            def mousePressEvent(self, event):
                if event.button() == Qt.MouseButton.LeftButton:
                    self._drag_pos = self._event_pos(event) - self._window.frameGeometry().topLeft()
                    event.accept()

            def mouseMoveEvent(self, event):
                if event.buttons() & Qt.MouseButton.LeftButton and not self._window.isMaximized():
                    self._window.move(self._event_pos(event) - self._drag_pos)
                    event.accept()

            def mouseDoubleClickEvent(self, event):
                if event.button() == Qt.MouseButton.LeftButton:
                    self._toggle_maximized()
                    event.accept()

        def _apply_windows_rounded_corners(qwindow):
            if os.name != "nt":
                return
            try:
                hwnd = int(qwindow.winId())
                preference = ctypes.c_int(2)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(33),
                    ctypes.byref(preference),
                    ctypes.sizeof(preference),
                )
            except Exception as exc:
                flog_kv("MAIN", "desktop_rounded_corner_unavailable", "debug", error=str(exc))

        _set_app_user_model_id()
        try:
            QCoreApplication.setApplicationName(APP_NAME)
            QCoreApplication.setOrganizationName("Cronus")
        except Exception:
            pass
        app_qt = QApplication.instance() or QApplication(sys.argv[:1])
        _kanit_ok = _load_bundled_kanit_fonts()
        try:
            if _kanit_ok:
                from PySide6.QtGui import QFont

                app_qt.setFont(QFont("Kanit", 9))
        except Exception as exc:
            flog_kv("MAIN", "desktop_font_apply_failed", "debug", error=str(exc))
        icon_path = resource_path("assets", APP_ICON_FILE)
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        if not icon.isNull():
            app_qt.setWindowIcon(icon)
        window = RoundedMainWindow()
        window.setWindowTitle(APP_NAME)
        window.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        window.setStyleSheet("QMainWindow { background: transparent; }")
        if not icon.isNull():
            window.setWindowIcon(icon)
        # Opaque WebView2 view: no translucency, no alpha compositing.
        # This is the main RAM/CPU win vs the old transparent QWebEngineView.
        view = QtWebView2Widget(parent=window, url=str(url or ""))
        view.setObjectName("CronusWebView")
        view.setStyleSheet("#CronusWebView { background: #0a0b10; border: 0; }")
        container = QWidget(window)
        container.setObjectName("CronusWindowShell")
        container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        container.setStyleSheet(
            """
            QWidget#CronusWindowShell {
background: #0a0b10;
                 border: 1px solid #20232c;
                border-radius: 10px;
            }
            """
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        title_bar = TitleBar(window)
        layout.addWidget(title_bar)
        layout.addWidget(view, 1)
        window.setCentralWidget(container)
        window.resize(1280, 820)
        _apply_windows_rounded_corners(window)
        window.show()
        window.raise_()
        window.activateWindow()
        try:
            if callable(on_first_show):
                on_first_show()
        except Exception:
            pass
        _apply_windows_rounded_corners(window)
        title_timer = QTimer(window)
        shutting_down = False

        def _stop_desktop_runtime(reason: str = "desktop_shutdown"):
            nonlocal shutting_down
            if shutting_down:
                return
            shutting_down = True
            try:
                shutdown_event.set()
            except Exception:
                pass
            try:
                title_timer.stop()
            except Exception:
                pass
            try:
                if farm is not None and bool(getattr(farm, "running", False)):
                    farm.stop()
            except Exception as exc:
                flog_kv("MAIN", "desktop_shutdown_stop_farm_failed", "warning", reason=reason, error=str(exc))
            try:
                from desktop.instance_guard import _clear_instance_state

                _clear_instance_state()
            except Exception:
                pass

        def _refresh_title_status():
            if shutting_down:
                return
            try:
                running = bool(getattr(farm, "running", False))
            except KeyboardInterrupt:
                _stop_desktop_runtime("ctrl_c")
                try:
                    app_qt.quit()
                except Exception:
                    pass
                return
            except Exception:
                running = False
            title_bar.set_running(running)

        title_timer.timeout.connect(_refresh_title_status)
        title_timer.start(500)
        window._cronus_title_timer = title_timer
        _refresh_title_status()
        from core import flog

        flog("[MAIN] Desktop window running (WebView2)")
        previous_sigint = signal.getsignal(signal.SIGINT)

        def _handle_sigint(_signum, _frame):
            from core import flog as _flog

            _flog("[MAIN] Ctrl+C received - closing desktop window")
            _stop_desktop_runtime("ctrl_c")
            app_qt.quit()

        try:
            signal.signal(signal.SIGINT, _handle_sigint)
        except Exception:
            previous_sigint = None
        try:
            app_qt.exec()
        except KeyboardInterrupt:
            from core import flog as _flog

            _flog("[MAIN] Ctrl+C received - closing desktop window")
            _stop_desktop_runtime("ctrl_c")
        finally:
            if previous_sigint is not None:
                try:
                    signal.signal(signal.SIGINT, previous_sigint)
                except Exception:
                    pass
            _stop_desktop_runtime("desktop_window_closed")
        return True
