from __future__ import annotations

import ctypes
import os
import signal
import sys
from typing import Any, Callable, Optional

from app_paths import APP_NAME, APP_ROOT_DIR, CACHE_DIR, resource_path
from version import app_display_version
from core import flog_kv
from desktop.console_icon import APP_ICON_FILE


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
        _prepare_webview_env()
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
        view.setStyleSheet("#CronusWebView { background: #0b0c10; border: 0; }")
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
