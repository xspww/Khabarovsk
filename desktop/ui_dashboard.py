from __future__ import annotations

from infra.app_paths import resource_path

_STATIC_SCRIPTS = (
    '<script src="/ui/runtime/zoomLock.js?v=1"></script>'
    '<script src="/ui/runtime/perfGuard.js?v=8"></script>'
    '<script src="/ui/runtime/robloxSettingsCard.js?v=1"></script>'
    '<script src="/ui/runtime/executorCompatibility.js?v=8"></script>'
    '<script src="/ui/runtime/gamesManager.js?v=16"></script>'
    '<script src="/ui/runtime/customUi.js?v=16"></script>'
    '<script src="/ui/runtime/cronusManager.js?v=7"></script>'
    '<script src="/ui/runtime/hoverScrollbar.js?v=4"></script>'
    '<script src="/ui/runtime/updateNotice.js?v=6"></script>'
)



def _load_html_ui() -> str:
    with open(resource_path("ui", "index.html"), encoding="utf-8") as f:
        html = f.read()
    if "</body>" in html:
        return html.replace("</body>", _STATIC_SCRIPTS + "</body>", 1)
    return html + _STATIC_SCRIPTS


# Lazy dashboard shell. CSS and JavaScript live under ui/ and are served by FastAPI.
# Loaded on first get_html_ui() call so importing this module stays cheap.
class _HtmlUiCache:
    def __init__(self) -> None:
        self.html: str | None = None

    def load(self) -> str:
        try:
            html = _load_html_ui()
            self.html = html
            return html
        except Exception:
            if self.html is not None:
                return self.html
            return _load_html_ui()


_HTML_UI_CACHE = _HtmlUiCache()


def get_html_ui() -> str:
    """Fresh HTML per call so edits to ui/index.html show up without a restart."""
    return _HTML_UI_CACHE.load()
