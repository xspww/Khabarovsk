from __future__ import annotations

import ctypes
import os
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from typing import Any, Tuple, Optional

import uvicorn

from app_paths import APP_NAME, APP_ROOT_DIR, resource_path
from version import app_display_version
from console_activity import format_console_line
from desktop import console_output
from core import LOG_FILE, flog, flog_kv
from desktop.console_icon import (
    APP_ICON_FILE,
    set_console_window_icon as _set_console_window_icon,
)
from desktop.instance_guard import (
    INSTANCE_TOKEN as _INSTANCE_TOKEN,
    _acquire_instance_socket,
    _acquire_single_instance_mutex,
    _clear_instance_state,
    _find_free_port,
    _stop_previous_instance,
    _stop_same_app_processes,
    _write_instance_state,
    clear_instance_state,
    prepare_backend_single_instance,
)

APP_USER_AGENT = "CronusLauncher/RT"
BASE_DIR = APP_ROOT_DIR
HOST = "127.0.0.1"
PORT = 7777
_SHUTDOWN_REQUESTED = threading.Event()
_BACKEND_THREAD_ERROR = ""
_app = None
_farm = None
_STARTUP_PROGRESS_WIDTH = 44
_STARTUP_PROGRESS_TOTAL_STEPS = 6
_STARTUP_PROGRESS_FRAME_DELAY = 0.012
_STARTUP_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_STARTUP_PROGRESS_ACTIVE = False
_STARTUP_PROGRESS_LAST_LEN = 0
_STARTUP_PROGRESS_PERCENT = 0
_STARTUP_SPINNER_INDEX = 0
_STARTUP_CLEAR_AFTER_WINDOW_SHOW = False
_STARTUP_COLOR_SUPPORT: Optional[bool] = None
_COLOR_DIM = "\x1b[90m"
_COLOR_NAVY_BLUE = "\x1b[38;2;30;64;175m"
_COLOR_NAVY_TEXT = "\x1b[38;2;96;165;250m"


def _console_write(message: str = "") -> None:
    console_output.write_line(message, {"→": "->"})


def _console_write_inline(message: str = "") -> None:
    console_output.write_inline(message, {"→": "->", "█": "#", "░": "."}, _STARTUP_SPINNER_FRAMES)


def _startup_enable_virtual_terminal() -> bool:
    return console_output.enable_virtual_terminal(sys.stdout)


def _startup_colors_enabled() -> bool:
    global _STARTUP_COLOR_SUPPORT
    if not console_output.color_requested():
        return False
    if _STARTUP_COLOR_SUPPORT is None:
        _STARTUP_COLOR_SUPPORT = _startup_enable_virtual_terminal()
    return bool(_STARTUP_COLOR_SUPPORT)


def _startup_paint(text: str, color: str) -> str:
    return console_output.paint(text, color, enabled=_startup_colors_enabled())


def _startup_visible_len(text: str) -> int:
    return console_output.visible_len(text)


def _startup_progress_step(percent: int) -> int:
    pct = max(0, min(100, int(percent)))
    if pct >= 96:
        return 6
    if pct >= 88:
        return 5
    if pct >= 78:
        return 4
    if pct >= 55:
        return 3
    if pct >= 35:
        return 2
    return 1


def _startup_progress_line(percent: int, detail: str) -> str:
    global _STARTUP_SPINNER_INDEX
    try:
        pct = int(percent)
    except Exception:
        pct = 0
    pct = max(0, min(100, pct))
    filled = int(round(_STARTUP_PROGRESS_WIDTH * (pct / 100.0)))
    bar = _startup_paint("█" * filled, _COLOR_NAVY_BLUE) + _startup_paint("░" * (_STARTUP_PROGRESS_WIDTH - filled), _COLOR_DIM)
    spinner = _STARTUP_SPINNER_FRAMES[_STARTUP_SPINNER_INDEX % len(_STARTUP_SPINNER_FRAMES)]
    _STARTUP_SPINNER_INDEX += 1
    detail_text = str(detail or "").strip() or "Starting"
    label = _startup_paint(detail_text, _COLOR_NAVY_TEXT)
    step = f"{_startup_progress_step(pct)}/{_STARTUP_PROGRESS_TOTAL_STEPS}"
    return f"{spinner} {label}  [{bar}] {step} {pct:3d}%"


def _render_startup_progress(percent: int, detail: str) -> None:
    global _STARTUP_PROGRESS_LAST_LEN
    line = _startup_progress_line(percent, detail)
    visible_len = _startup_visible_len(line)
    padding = " " * max(0, _STARTUP_PROGRESS_LAST_LEN - visible_len)
    _console_write_inline(f"\r{line}{padding}")
    _STARTUP_PROGRESS_LAST_LEN = visible_len


def _console_startup_progress(percent: int, detail: str) -> None:
    global _STARTUP_PROGRESS_ACTIVE, _STARTUP_PROGRESS_PERCENT
    try:
        target = int(percent)
    except Exception:
        target = 0
    target = max(0, min(100, target))
    start = _STARTUP_PROGRESS_PERCENT if _STARTUP_PROGRESS_ACTIVE else target
    if _STARTUP_PROGRESS_ACTIVE and target > start:
        frame_count = min(10, max(3, target - start))
        for index in range(1, frame_count + 1):
            frame_percent = start + round((target - start) * (index / frame_count))
            _render_startup_progress(frame_percent, detail)
            if index < frame_count:
                time.sleep(_STARTUP_PROGRESS_FRAME_DELAY)
    else:
        _render_startup_progress(target, detail)
    _STARTUP_PROGRESS_ACTIVE = True
    _STARTUP_PROGRESS_PERCENT = target


def _console_clear_startup_screen() -> None:
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass


def _console_finish_startup(*, clear: bool) -> None:
    global _STARTUP_PROGRESS_ACTIVE, _STARTUP_PROGRESS_LAST_LEN, _STARTUP_PROGRESS_PERCENT, _STARTUP_SPINNER_INDEX
    if _STARTUP_PROGRESS_ACTIVE:
        _console_write_inline("\r" + (" " * _STARTUP_PROGRESS_LAST_LEN) + "\r")
    _STARTUP_PROGRESS_ACTIVE = False
    _STARTUP_PROGRESS_LAST_LEN = 0
    _STARTUP_PROGRESS_PERCENT = 0
    _STARTUP_SPINNER_INDEX = 0
    if clear:
        _console_clear_startup_screen()


def _console_clear_after_window_show(enabled: bool = True) -> None:
    global _STARTUP_CLEAR_AFTER_WINDOW_SHOW
    _STARTUP_CLEAR_AFTER_WINDOW_SHOW = bool(enabled)


def _console_finish_after_window_show() -> None:
    global _STARTUP_CLEAR_AFTER_WINDOW_SHOW
    clear = bool(_STARTUP_CLEAR_AFTER_WINDOW_SHOW)
    _STARTUP_CLEAR_AFTER_WINDOW_SHOW = False
    _console_finish_startup(clear=clear)


def _console_event(icon: str, message: str, *, indent: bool = False) -> None:
    _console_write(format_console_line(icon, message, indent=indent))


def _console_status(label: str, detail: str) -> None:
    label_key = str(label or "").strip().lower()
    detail_text = str(detail or "").strip()
    if label_key == "startup":
        if "existing" in detail_text.lower():
            _console_startup_progress(18, detail_text)
        else:
            _console_startup_progress(8, detail_text or "Preparing startup")
        return
    if label_key == "port":
        _console_startup_progress(35, detail_text or "Selecting local port")
        return
    if label_key == "backend":
        if detail_text.lower().startswith("not ready"):
            _console_finish_startup(clear=False)
            _console_event("XX", f"Cronus backend not ready: {detail_text.replace('Not ready:', '', 1).strip()}")
        elif detail_text.lower().startswith("ready"):
            _console_startup_progress(78, detail_text)
        else:
            _console_startup_progress(55, detail_text or "Starting FastAPI server")
        return
    if label_key == "dashboard":
        _console_startup_progress(88, detail_text or "Dashboard ready")
        return
    if label_key == "desktop":
        _console_startup_progress(96, detail_text or "Opening desktop window")
        return
    if label_key == "shutdown":
        return
    if label_key == "log":
        return
    return


def _console_header() -> None:
    os.environ.setdefault("CRONUS_CONSOLE_ACTIVITY", "1")
    os.environ.setdefault("CRONUS_CONSOLE_COLOR", "1")
    try:
        if os.name == "nt":
            ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} Console")
            _set_console_window_icon()
    except Exception:
        pass
    return


def configure(fastapi_app: Any, farm_controller: Any) -> None:
    global _app, _farm
    _app = fastapi_app
    _farm = farm_controller


def _require_configured() -> Tuple[Any, Any]:
    if _app is None or _farm is None:
        raise RuntimeError("desktop_host is not configured")
    return _app, _farm

def _make_tray_icon():
    try:
        from PIL import Image
        icon_path = resource_path("assets", APP_ICON_FILE)
        if os.path.exists(icon_path):
            return Image.open(icon_path)
        return Image.new("RGBA", (64, 64), (0, 0, 0, 255))
    except ImportError:
        return None

def _set_app_user_model_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Cronus.Launcher.Desktop")
    except Exception:
        pass

def _bundled_kanit_candidates() -> list:
    candidates: list = []
    for name in ("Kanit-Regular.ttf", "Kanit-Medium.ttf"):
        for base in {
            resource_path("assets", "fonts", name),
            os.path.join(APP_ROOT_DIR, "assets", "fonts", name),
        }:
            if base and base not in candidates:
                candidates.append(base)
    return candidates

def _load_bundled_kanit_fonts() -> bool:
    """Zero-install Kanit for Qt widgets (TitleBar). Web UI uses @font-face separately.

    Qt cannot use woff2/web @font-face, so ship converted TTFs under assets/fonts/
    and register them with QFontDatabase. No system install, no admin needed.
    Returns True when the 'Kanit' family is usable (bundled or pre-installed).
    """
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
            else:
                flog_kv("MAIN", "desktop_font_load_failed", "warning", path=path, font_id=font_id)
        except Exception as exc:
            flog_kv("MAIN", "desktop_font_load_failed", "warning", path=path, error=str(exc))
    try:
        if "Kanit" in QFontDatabase.families():
            if loaded_any:
                flog("[MAIN] Bundled Kanit fonts loaded for Qt widgets")
            return True
    except Exception:
        pass
    if not loaded_any:
        flog_kv("MAIN", "desktop_font_missing", "warning", hint="assets/fonts/Kanit-*.ttf not found; Qt falls back to system fonts")
    return False

def _run_backend_server() -> None:
    global _BACKEND_THREAD_ERROR
    try:
        uvicorn.run(_require_configured()[0], host=HOST, port=PORT, log_level="warning", access_log=False, log_config=None)
        if not _SHUTDOWN_REQUESTED.is_set():
            _BACKEND_THREAD_ERROR = "uvicorn returned before shutdown"
            flog_kv("MAIN", "fastapi_thread_exited", "error", port=PORT)
    except BaseException as exc:
        _BACKEND_THREAD_ERROR = f"{type(exc).__name__}: {exc}"
        flog_kv(
            "MAIN",
            "fastapi_thread_failed",
            "error",
            port=PORT,
            error=_BACKEND_THREAD_ERROR,
            traceback=traceback.format_exc(),
        )

def _start_backend_thread() -> threading.Thread:
    server_thread = threading.Thread(
        target=_run_backend_server,
        daemon=True,
        name="UvicornServer",
    )
    server_thread.start()
    return server_thread

def _wait_for_backend_ready(server_thread: threading.Thread, timeout: float = 20.0) -> Tuple[bool, str]:
    wait_seconds = max(1.0, float(timeout or 20.0))
    started_at = time.time()
    deadline = started_at + wait_seconds
    url = f"http://{HOST}:{PORT}/api/status"
    last_error = ""
    last_progress = -1
    while time.time() < deadline:
        elapsed_ratio = min(1.0, max(0.0, (time.time() - started_at) / wait_seconds))
        progress = 56 + int(18 * elapsed_ratio)
        if progress != last_progress:
            _console_startup_progress(progress, "Waiting for FastAPI backend")
            last_progress = progress
        if not server_thread.is_alive():
            return False, _BACKEND_THREAD_ERROR or "backend thread exited before ready"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": APP_USER_AGENT})
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                if 200 <= int(resp.status) < 500:
                    return True, f"status={resp.status}"
        except urllib.error.HTTPError as exc:
            if 200 <= int(exc.code) < 500:
                return True, f"status={exc.code}"
            last_error = f"HTTPError: {exc.code}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    if not server_thread.is_alive():
        return False, _BACKEND_THREAD_ERROR or "backend thread exited before ready"
    return False, last_error or "backend readiness timeout"

def _run_desktop_window() -> bool:
    try:
        from PySide6.QtCore import QPoint, QSize, QTimer, Qt, QUrl
        from PySide6.QtGui import QBitmap, QColor, QIcon, QPainter, QPen, QPixmap
        from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except Exception as exc:
        flog_kv("MAIN", "desktop_qt_unavailable", "warning", error=str(exc))
        return False

    WINDOW_RADIUS = 10
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
        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._refresh_window_mask()

        def changeEvent(self, event):
            super().changeEvent(event)
            self._refresh_window_mask()

        def _refresh_window_mask(self):
            if self.isMaximized() or self.isFullScreen():
                self.clearMask()
                return
            mask = QBitmap(self.size())
            mask.fill(Qt.GlobalColor.color0)
            painter = QPainter(mask)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(Qt.GlobalColor.color1)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(self.rect(), WINDOW_RADIUS, WINDOW_RADIUS)
            painter.end()
            self.setMask(mask)

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
            # Centered title overlay: it fills the whole bar and is drawn behind
            # the logo (left) and window controls (right), which stay in place.
            self._title_label = QLabel(self)
            self._title_label.setObjectName("CronusTitle")
            self._title_label.setTextFormat(Qt.TextFormat.RichText)
            self._title_label.setText(f'<span>{APP_NAME}</span> <span style="color: #42495d;">- {app_display_version()}</span>')
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
                    background-color: #0d0e12;
                    border-bottom: 1px solid #1d1f26;
                    border-top-left-radius: 10px;
                    border-top-right-radius: 10px;
                }
                #CronusTitle {
                    font-family: "Kanit", "Segoe UI", "Leelawadee UI", Tahoma, "Noto Sans Thai", sans-serif;
                    color: #7f838c;
                    font-size: 12px;
                    font-weight: 500;
                }
                #CronusStatusIcon {
                    margin-right: 0px;
                }
                QPushButton#WinMinButton, QPushButton#WinMaxButton, QPushButton#WinCloseButton {
                    width: 34px; height: 22px; min-width: 34px; max-width: 34px;
                    min-height: 22px; max-height: 22px; border-radius: 9px;
                    border: 1px solid #232529;
                    background-color: #15161b;
                    color: #53565e;
                    padding: 0px;
                }
                QPushButton#WinMinButton:hover, QPushButton#WinMaxButton:hover {
                    background-color: #1b1c22;
                    border-color: #32343d;
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
    view = QWebEngineView(window)
    view.setObjectName("CronusWebView")
    view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    view.setStyleSheet("#CronusWebView { background: transparent; border: 0; }")
    try:
        view.page().setBackgroundColor(QColor(0, 0, 0, 0))
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_transparency_unavailable", "debug", error=str(exc))
    view.setUrl(QUrl(f"http://{HOST}:{PORT}"))
    container = QWidget(window)
    container.setObjectName("CronusWindowShell")
    container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    container.setStyleSheet(
        """
        QWidget#CronusWindowShell {
            background: #0b0c10;
            border: 1px solid #1d1f26;
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
    _console_finish_after_window_show()
    _apply_windows_rounded_corners(window)
    title_timer = QTimer(window)
    shutting_down = False

    def _stop_desktop_runtime(reason: str = "desktop_shutdown"):
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        _SHUTDOWN_REQUESTED.set()
        try:
            title_timer.stop()
        except Exception:
            pass
        try:
            farm = _require_configured()[1]
            if farm.running:
                farm.stop()
        except Exception as exc:
            flog_kv("MAIN", "desktop_shutdown_stop_farm_failed", "warning", reason=reason, error=str(exc))
        _clear_instance_state()

    def _refresh_title_status():
        if shutting_down:
            return
        try:
            running = bool(getattr(_require_configured()[1], "running", False))
        except KeyboardInterrupt:
            flog("[MAIN] Ctrl+C received - closing desktop window")
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
    flog("[MAIN] Desktop window running")
    previous_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(_signum, _frame):
        flog("[MAIN] Ctrl+C received - closing desktop window")
        _stop_desktop_runtime("ctrl_c")
        app_qt.quit()

    try:
        signal.signal(signal.SIGINT, _handle_sigint)
    except Exception:
        previous_sigint = None
    try:
        app_qt.exec()
    except KeyboardInterrupt:
        flog("[MAIN] Ctrl+C received - closing desktop window")
        _stop_desktop_runtime("ctrl_c")
    finally:
        if previous_sigint is not None:
            try:
                signal.signal(signal.SIGINT, previous_sigint)
            except Exception:
                pass
        _stop_desktop_runtime("desktop_window_closed")
    return True

def run_desktop(fastapi_app: Any = None, farm_controller: Any = None):
    if fastapi_app is not None or farm_controller is not None:
        configure(fastapi_app, farm_controller)
    global PORT
    _console_header()
    _console_status("startup", "Preparing single-instance guard")
    _stop_previous_instance()
    _stop_same_app_processes()
    mutex_ok = _acquire_single_instance_mutex()
    socket_ok = _acquire_instance_socket()
    if (not mutex_ok) or (not socket_ok):
        _console_status("startup", "Existing Cronus instance detected; requesting cleanup")
        _stop_previous_instance()
        _stop_same_app_processes()
        if not socket_ok:
            socket_ok = _acquire_instance_socket()
    PORT = _find_free_port(7777)
    _console_status("port", f"Selected http://{HOST}:{PORT}")
    _write_instance_state(PORT)
    _console_status("backend", "Starting FastAPI server")
    server_thread = _start_backend_thread()
    ready, detail = _wait_for_backend_ready(server_thread)
    if ready:
        flog(f"[MAIN] FastAPI ready on http://{HOST}:{PORT}")
        _console_status("backend", f"Ready ({detail})")
        _console_status("dashboard", f"http://{HOST}:{PORT}")
    else:
        flog_kv("MAIN", "fastapi_not_ready", "error", port=PORT, detail=detail)
        _console_status("backend", f"Not ready: {detail}")
        _console_status("log", LOG_FILE)
    _console_status("desktop", "Opening desktop window")
    _console_clear_after_window_show(ready)
    if _run_desktop_window():
        _console_status("shutdown", "Cronus window closed")
        return
    _console_status("desktop", "Desktop window unavailable; opening browser fallback")
    _console_clear_after_window_show(False)
    _console_finish_startup(clear=ready)
    webbrowser.open(f"http://{HOST}:{PORT}")
    try:
        while not _SHUTDOWN_REQUESTED.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        _console_status("shutdown", "Ctrl+C received; stopping farm")
        farm = _require_configured()[1]
        if farm.running:
            farm.stop()
        _clear_instance_state()
        sys.exit(0)
INSTANCE_TOKEN = _INSTANCE_TOKEN
SHUTDOWN_REQUESTED = _SHUTDOWN_REQUESTED
clear_instance_state = _clear_instance_state
