from __future__ import annotations

from app_paths import resource_path

_STATIC_SCRIPTS = (
    '<script src="/ui/runtime/perfGuard.js?v=2"></script>'
    '<script src="/ui/runtime/exploitstrapVersionPicker.js?v=7"></script>'
    '<script src="/ui/runtime/executorCompatibility.js?v=8"></script>'
    '<script src="/ui/runtime/gamesManager.js?v=13"></script>'
    '<script src="/ui/runtime/customUi.js?v=13"></script>'
    '<script src="/ui/runtime/cronusManager.js?v=2"></script>'
    '<script src="/ui/runtime/updateNotice.js?v=5"></script>'
)



def _load_html_ui() -> str:
    with open(resource_path("ui", "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    if "</body>" in html:
        return html.replace("</body>", _STATIC_SCRIPTS + "</body>", 1)
    return html + _STATIC_SCRIPTS


# Lazy dashboard shell. CSS and JavaScript live under ui/ and are served by FastAPI.
# Loaded on first get_html_ui() call so importing this module stays cheap.
_HTML_UI_CACHE: str | None = None


def get_html_ui() -> str:
    """Fresh HTML per call so edits to ui/index.html show up without a restart."""
    global _HTML_UI_CACHE
    try:
        html = _load_html_ui()
        _HTML_UI_CACHE = html
        return html
    except Exception:
        if _HTML_UI_CACHE is not None:
            return _HTML_UI_CACHE
        return _load_html_ui()
