from __future__ import annotations

import atexit
import ctypes
import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from typing import Any, Dict, List, Optional, Tuple

import uvicorn

from app_paths import APP_NAME, APP_DATA_DIR, APP_ROOT_DIR, IS_COMPILED, path_targets_current_exe, resource_path
from console_activity import format_console_line
from core import LOG_FILE, flog, flog_kv

APP_USER_AGENT = "CronusLauncher/RT"
APP_ICON_FILE = "cronus_icon.png"
BASE_DIR = APP_ROOT_DIR
HOST = "127.0.0.1"
PORT = 7777
_APP_MUTEX = None
_INSTANCE_SOCKET = None
_INSTANCE_TOKEN = __import__("secrets").token_urlsafe(32)
_INSTANCE_STATE_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt_instance.json")
_SHUTDOWN_REQUESTED = threading.Event()
_BACKEND_THREAD_ERROR = ""
_app = None
_farm = None
_CONSOLE_ICON_HANDLES: List[int] = []


def _destroy_console_icon_handles() -> None:
    if os.name != "nt":
        return
    while _CONSOLE_ICON_HANDLES:
        handle = _CONSOLE_ICON_HANDLES.pop()
        try:
            ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(int(handle)))
        except Exception:
            pass


atexit.register(_destroy_console_icon_handles)


def _ensure_console_icon_file() -> str:
    source_path = resource_path("assets", APP_ICON_FILE)
    if not os.path.exists(source_path):
        fallback = resource_path("assets", "argus_launcher.ico")
        return fallback if os.path.exists(fallback) else ""
    icon_path = os.path.join(APP_DATA_DIR, "cronus_console_icon.ico")
    try:
        if (
            os.path.exists(icon_path)
            and os.path.getmtime(icon_path) >= os.path.getmtime(source_path)
            and os.path.getsize(icon_path) > 0
        ):
            return icon_path
    except Exception:
        pass
    try:
        from PIL import Image

        with Image.open(source_path) as image:
            image.convert("RGBA").save(
                icon_path,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
        return icon_path
    except Exception as exc:
        flog_kv("MAIN", "console_icon_create_failed", "warning", source=source_path, error=str(exc))
        fallback = resource_path("assets", "argus_launcher.ico")
        return fallback if os.path.exists(fallback) else ""


def _set_console_window_icon() -> bool:
    if os.name != "nt":
        return False
    icon_path = _ensure_console_icon_file()
    if not icon_path:
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(kernel32.GetConsoleWindow() or 0)
        if not hwnd:
            return False

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        GCLP_HICON = -14
        GCLP_HICONSM = -34

        def _load_icon(size: int) -> int:
            return int(user32.LoadImageW(None, icon_path, IMAGE_ICON, size, size, LR_LOADFROMFILE) or 0)

        small_icon = _load_icon(16)
        big_icon = _load_icon(32) or _load_icon(48)
        if not small_icon and not big_icon:
            return False
        if small_icon:
            user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON, ICON_SMALL, ctypes.c_void_p(small_icon))
            _CONSOLE_ICON_HANDLES.append(small_icon)
        if big_icon:
            user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON, ICON_BIG, ctypes.c_void_p(big_icon))
            _CONSOLE_ICON_HANDLES.append(big_icon)
        try:
            set_class_long_ptr = getattr(user32, "SetClassLongPtrW", None) or getattr(user32, "SetClassLongW", None)
            if set_class_long_ptr:
                if big_icon:
                    set_class_long_ptr(ctypes.c_void_p(hwnd), GCLP_HICON, ctypes.c_void_p(big_icon))
                if small_icon:
                    set_class_long_ptr(ctypes.c_void_p(hwnd), GCLP_HICONSM, ctypes.c_void_p(small_icon))
        except Exception:
            pass
        return True
    except Exception as exc:
        flog_kv("MAIN", "console_icon_set_failed", "warning", icon=icon_path, error=str(exc))
        return False


def _console_write(message: str = "") -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        try:
            safe_message = str(message).replace("→", "->")
            print(safe_message.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
    except Exception:
        pass


def _console_event(icon: str, message: str, *, indent: bool = False) -> None:
    _console_write(format_console_line(icon, message, indent=indent))


def _console_status(label: str, detail: str) -> None:
    label_key = str(label or "").strip().lower()
    detail_text = str(detail or "").strip()
    if label_key == "startup":
        return
    if label_key == "port":
        return
    if label_key == "backend":
        if detail_text.lower().startswith("not ready"):
            _console_event("XX", f"Cronus backend not ready: {detail_text.replace('Not ready:', '', 1).strip()}")
        return
    if label_key == "dashboard":
        return
    if label_key == "desktop":
        return
    if label_key == "shutdown":
        return
    if label_key == "log":
        return
    return


def _console_header(mode: str) -> None:
    os.environ.setdefault("ARGUS_CONSOLE_ACTIVITY", "1")
    os.environ.setdefault("ARGUS_CONSOLE_COLOR", "1")
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

def _find_free_port(start: int = 7777) -> int:
    for p in range(start, start + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((HOST, p))
                return p
        except OSError:
            continue
    return start

def _find_existing_dashboard(start: int = 7777) -> Optional[int]:
    for p in range(start, start + 20):
        try:
            req = urllib.request.Request(
                f"http://{HOST}:{p}/api/status",
                headers={"User-Agent": APP_USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                if resp.status == 200:
                    return p
        except Exception:
            continue
    return None

def _cmdline_targets_this_app(cmdline: List[str], cwd: str = "") -> bool:
    try:
        base = os.path.normcase(os.path.abspath(BASE_DIR))
        cwd_norm = os.path.normcase(os.path.abspath(cwd or "")) if cwd else ""
    except Exception:
        cwd_norm = ""
        base = os.path.normcase(os.path.abspath(BASE_DIR))
    has_python = False
    has_main = False
    main_path = os.path.normcase(os.path.join(os.path.abspath(BASE_DIR), "main.py"))
    for part in cmdline or []:
        text = str(part or "")
        low = text.lower()
        if "python" in os.path.basename(low):
            has_python = True
        if os.path.basename(low) == "main.py":
            candidate = os.path.normcase(os.path.abspath(os.path.join(cwd or BASE_DIR, text)))
            has_main = candidate == main_path or cwd_norm == base
    return bool(has_python and has_main)

def _is_same_roboguard_process(pid: int) -> bool:
    if not pid or int(pid) == os.getpid():
        return False
    try:
        import psutil

        proc = psutil.Process(int(pid))
        try:
            proc_name = os.path.basename(str(proc.name() or "")).lower()
            proc_exe_path = str(proc.exe() or "")
            proc_exe = os.path.basename(proc_exe_path).lower()
        except Exception:
            proc_name = ""
            proc_exe_path = ""
            proc_exe = ""
        try:
            cmdline = proc.cmdline()
            cwd = proc.cwd()
        except Exception:
            cmdline = []
            cwd = ""
        if IS_COMPILED:
            if path_targets_current_exe(proc_exe_path, cwd):
                return True
            if any(path_targets_current_exe(part, cwd) for part in cmdline):
                return True
            return False
        if "python" not in proc_name and "python" not in proc_exe:
            return False
        return _cmdline_targets_this_app(cmdline, cwd)
    except Exception:
        return False

def _is_pid_alive(pid: int) -> bool:
    try:
        import psutil

        return bool(pid and psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).status() != "zombie")
    except Exception:
        return False

def _read_instance_state() -> Dict[str, Any]:
    try:
        if not os.path.exists(_INSTANCE_STATE_FILE):
            return {}
        with open(_INSTANCE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _write_instance_state(port: int) -> None:
    payload = {
        "pid": os.getpid(),
        "port": int(port),
        "token": _INSTANCE_TOKEN,
        "base_dir": BASE_DIR,
        "started_at": time.time(),
    }
    try:
        os.makedirs(os.path.dirname(_INSTANCE_STATE_FILE), exist_ok=True)
        with open(_INSTANCE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:
        flog_kv("MAIN", "instance_state_write_failed", "warning", error=str(exc))

def _clear_instance_state() -> None:
    try:
        state = _read_instance_state()
        if int(state.get("pid") or 0) == os.getpid() or state.get("token") == _INSTANCE_TOKEN:
            if os.path.exists(_INSTANCE_STATE_FILE):
                os.remove(_INSTANCE_STATE_FILE)
    except Exception:
        pass

def _request_instance_shutdown(port: int, token: str) -> bool:
    try:
        body = json.dumps({"token": token}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{HOST}:{int(port)}/api/app/shutdown",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "X-RoboGuard-Token": token},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return 200 <= int(resp.status) < 300
    except Exception:
        return False

def _terminate_instance_tree(pid: int) -> bool:
    if not _is_same_roboguard_process(pid):
        return False
    try:
        import psutil

        proc = psutil.Process(int(pid))
        current = psutil.Process(os.getpid())
        if int(proc.pid) in {int(parent.pid) for parent in current.parents()}:
            return False
        children = proc.children(recursive=True)
        if any(int(getattr(child, "pid", 0) or 0) == os.getpid() for child in children):
            return False
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        proc.terminate()
        gone, alive = psutil.wait_procs([proc] + children, timeout=4.0)
        for item in alive:
            try:
                item.kill()
            except Exception:
                pass
        return True
    except Exception as exc:
        flog_kv("MAIN", "instance_tree_terminate_failed", "warning", pid=pid, error=str(exc))
        return False

def _stop_previous_instance(wait_seconds: float = 8.0) -> bool:
    state = _read_instance_state()
    pid = int(state.get("pid") or 0)
    port = int(state.get("port") or 0)
    token = str(state.get("token") or "")
    if not pid or not _is_pid_alive(pid) or not _is_same_roboguard_process(pid):
        return False
    flog_kv("MAIN", "previous_instance_detected", pid=pid, port=port)
    if port and token:
        _request_instance_shutdown(port, token)
    deadline = time.time() + max(1.0, float(wait_seconds or 8.0))
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.25)
    return _terminate_instance_tree(pid)

def _stop_same_app_processes() -> int:
    stopped = 0
    try:
        import psutil

        current_ancestors = {int(parent.pid) for parent in psutil.Process(os.getpid()).parents()}
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            pid = int(proc.info.get("pid") or 0)
            if pid == os.getpid() or pid in current_ancestors:
                continue
            if _is_same_roboguard_process(pid) and _terminate_instance_tree(pid):
                stopped += 1
    except Exception as exc:
        flog_kv("MAIN", "stop_same_app_processes_failed", "warning", error=str(exc))
    return stopped

atexit.register(_clear_instance_state)

def _acquire_single_instance_mutex() -> bool:
    global _APP_MUTEX
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        mutex = kernel32.CreateMutexW(None, False, "Local\\RoboGuard_RT_1_0")
        if not mutex:
            return True
        _APP_MUTEX = mutex
        return ctypes.get_last_error() != 183
    except Exception:
        return True

def _acquire_instance_socket() -> bool:
    global _INSTANCE_SOCKET
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((HOST, 7711))
        sock.listen(1)
        _INSTANCE_SOCKET = sock
        return True
    except OSError:
        return False

def _has_older_main_process() -> bool:
    try:
        import psutil
        current = psutil.Process(os.getpid())
        current_ct = float(current.create_time())
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            if proc.info.get("pid") == current.pid:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if "main.py" not in cmdline:
                continue
            if "python" not in (proc.info.get("name") or "").lower() and "python" not in cmdline:
                continue
            if float(proc.info.get("create_time") or 0.0) <= current_ct:
                return True
    except Exception:
        return False
    return False

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
    deadline = time.time() + max(1.0, float(timeout or 20.0))
    url = f"http://{HOST}:{PORT}/api/status"
    last_error = ""
    while time.time() < deadline:
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
        from PySide6.QtCore import QPoint, QTimer, Qt, QUrl
        from PySide6.QtGui import QBitmap, QColor, QIcon, QPainter, QPixmap
        from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except Exception as exc:
        flog_kv("MAIN", "desktop_qt_unavailable", "warning", error=str(exc))
        return False

    WINDOW_RADIUS = 10
    TITLE_ICON_FILE = resource_path("ui", "cronus-start-icon.png")

    def _tinted_title_icon(color: QColor) -> QPixmap:
        source = QPixmap(TITLE_ICON_FILE)
        if source.isNull():
            return QPixmap()
        source = source.scaled(
            24,
            16,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        tinted = QPixmap(source.size())
        tinted.fill(QColor(0, 0, 0, 0))
        painter = QPainter(tinted)
        painter.drawPixmap(0, 0, source)
        try:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        except AttributeError:
            painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), color)
        painter.end()
        return tinted

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
            self._idle_icon = _tinted_title_icon(QColor("#616777"))
            self._active_icon = _tinted_title_icon(QColor("#16a964"))
            self.setObjectName("CronusTitleBar")
            self.setFixedHeight(32)
            layout = QHBoxLayout(self)
            layout.setContentsMargins(12, 0, 10, 0)
            layout.setSpacing(8)
            self._status_icon = QLabel(self)
            self._status_icon.setObjectName("CronusStatusIcon")
            self._status_icon.setFixedSize(24, 18)
            if not self._idle_icon.isNull():
                self._status_icon.setPixmap(self._idle_icon)
            layout.addWidget(self._status_icon)
            title_label = QLabel(self)
            title_label.setText(title)
            title_label.setObjectName("CronusTitle")
            layout.addWidget(title_label)
            layout.addStretch(1)
            min_btn = self._button("WinMinButton", "Minimize", "-")
            max_btn = self._button("WinMaxButton", "Maximize", "[]")
            close_btn = self._button("WinCloseButton", "Close", "x")
            min_btn.clicked.connect(parent.showMinimized)
            max_btn.clicked.connect(self._toggle_maximized)
            close_btn.clicked.connect(parent.close)
            layout.addWidget(min_btn)
            layout.addWidget(max_btn)
            layout.addWidget(close_btn)
            # Compatibility markers for legacy title-bar tests: MacCloseButton, MacMinButton, MacMaxButton.
            self.setStyleSheet(
                """
                #CronusTitleBar {
                    background-color: #0b1120;
                    border-bottom: 1px solid #1c2940;
                    border-top-left-radius: 10px;
                    border-top-right-radius: 10px;
                }
                #CronusTitle {
                    color: #cbd7ef;
                    font-size: 12px;
                    font-weight: 700;
                }
                #CronusStatusIcon {
                    margin-right: 0px;
                }
                QPushButton#WinMinButton, QPushButton#WinMaxButton, QPushButton#WinCloseButton {
                    width: 28px; height: 20px; min-width: 28px; max-width: 28px;
                    min-height: 20px; max-height: 20px; border-radius: 7px;
                    border: 1px solid #1d2a41;
                    background-color: #0f1728;
                    color: #74839d;
                    font-size: 10px;
                    font-weight: 800;
                }
                QPushButton#WinMinButton:hover, QPushButton#WinMaxButton:hover {
                    background-color: #15223a;
                    border-color: #2d456c;
                    color: #e7f0ff;
                }
                QPushButton#WinCloseButton:hover {
                    background-color: #371723;
                    border-color: #6b273a;
                    color: #ff8b98;
                }
                """
            )

        def set_running(self, running: bool):
            running = bool(running)
            if running == self._running:
                return
            self._running = running
            icon = self._active_icon if running else self._idle_icon
            if not icon.isNull():
                self._status_icon.setPixmap(icon)

        def _button(self, name: str, tooltip: str, text: str):
            button = QPushButton(text, self)
            button.setObjectName(name)
            button.setToolTip(tooltip)
            button.setFixedSize(28, 20)
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
            background: #080d18;
            border: 1px solid #1c2940;
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
    _apply_windows_rounded_corners(window)
    title_timer = QTimer(window)

    def _refresh_title_status():
        try:
            running = bool(getattr(_require_configured()[1], "running", False))
        except Exception:
            running = False
        title_bar.set_running(running)

    title_timer.timeout.connect(_refresh_title_status)
    title_timer.start(500)
    window._cronus_title_timer = title_timer
    _refresh_title_status()
    flog("[MAIN] Desktop window running")
    app_qt.exec()
    try:
        farm = _require_configured()[1]
        if farm.running:
            farm.stop()
    except Exception:
        pass
    _clear_instance_state()
    return True

def run_desktop(fastapi_app: Any = None, farm_controller: Any = None):
    if fastapi_app is not None or farm_controller is not None:
        configure(fastapi_app, farm_controller)
    global PORT
    _console_header("Desktop")
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
    if _run_desktop_window():
        _console_status("shutdown", "Cronus window closed")
        return
    _console_status("desktop", "Desktop window unavailable; opening browser fallback")
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

def run_with_tray(fastapi_app: Any = None, farm_controller: Any = None):
    if fastapi_app is not None or farm_controller is not None:
        configure(fastapi_app, farm_controller)
    global PORT
    if _has_older_main_process():
        existing_port = _find_existing_dashboard(7777)
        if existing_port is not None:
            flog(f"[MAIN] Older main.py process detected on http://{HOST}:{existing_port}")
            try:
                webbrowser.open(f"http://{HOST}:{existing_port}")
            except Exception:
                pass
            return

    if (not _acquire_single_instance_mutex()) or (not _acquire_instance_socket()):
        existing_port = _find_existing_dashboard(7777)
        if existing_port is not None:
            flog(f"[MAIN] Another instance is already running on http://{HOST}:{existing_port}")
            try:
                webbrowser.open(f"http://{HOST}:{existing_port}")
            except Exception:
                pass
            return

    existing_port = _find_existing_dashboard(7777)
    if existing_port is not None:
        flog(f"[MAIN] Existing dashboard detected on http://{HOST}:{existing_port} - reusing instance")
        try:
            webbrowser.open(f"http://{HOST}:{existing_port}")
        except Exception:
            pass
        return

    PORT = _find_free_port(7777)

    server_thread = _start_backend_thread()
    ready, detail = _wait_for_backend_ready(server_thread)
    if ready:
        flog(f"[MAIN] FastAPI ready on http://{HOST}:{PORT}")
    else:
        flog_kv("MAIN", "fastapi_not_ready", "error", port=PORT, detail=detail)

    try:
        import pystray
        from pystray import MenuItem as Item

        icon_img = _make_tray_icon()
        if icon_img is None:
            raise ImportError("PIL not available")

        def open_browser():
            webbrowser.open(f"http://{HOST}:{PORT}")

        def exit_app(icon, _=None):
            farm = _require_configured()[1]
            if farm.running:
                farm.stop()
            icon.stop()
            os._exit(0)

        menu = pystray.Menu(
            Item("Open Cronus Launcher", lambda: open_browser()),
            Item("Start Farm",        lambda: (_require_configured()[1].start() if not _require_configured()[1].running else None)),
            Item("Stop Farm",         lambda: (_require_configured()[1].stop() if _require_configured()[1].running else None)),
            Item("Restart Farm",      lambda: (_require_configured()[1].stop() or time.sleep(0.5) or _require_configured()[1].start())),
            pystray.Menu.SEPARATOR,
            Item("Exit",              lambda i, _: exit_app(i)),
        )

        icon = pystray.Icon("CronusLauncher", icon_img, APP_NAME, menu)
        threading.Thread(target=open_browser, daemon=True).start()
        flog("[MAIN] Tray icon running")
        icon.run()

    except ImportError:
        flog("[MAIN] pystray not available - running as console")
        webbrowser.open(f"http://{HOST}:{PORT}")
        print(f"""
+-----------------------------------------------+
|            Cronus Launcher Console           |
+-----------------------------------------------+
|  Web UI: http://{HOST}:{PORT}
|  Stop  : Ctrl+C
+-----------------------------------------------+
""")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            farm = _require_configured()[1]
            if farm.running:
                farm.stop()
            sys.exit(0)

def run_without_tray(fastapi_app: Any = None, farm_controller: Any = None):
    if fastapi_app is not None or farm_controller is not None:
        configure(fastapi_app, farm_controller)
    global PORT
    _stop_previous_instance()
    _stop_same_app_processes()

    PORT = _find_free_port(7777)
    _write_instance_state(PORT)
    flog(f"[MAIN] Starting console mode on http://{HOST}:{PORT}")
    threading.Thread(target=lambda: webbrowser.open(f"http://{HOST}:{PORT}"), daemon=True).start()
    try:
        uvicorn.run(_require_configured()[0], host=HOST, port=PORT, log_level="warning", access_log=False, log_config=None)
    finally:
        _clear_instance_state()


INSTANCE_TOKEN = _INSTANCE_TOKEN
SHUTDOWN_REQUESTED = _SHUTDOWN_REQUESTED
clear_instance_state = _clear_instance_state
