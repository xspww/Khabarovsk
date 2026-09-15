from __future__ import annotations

from app_paths import resource_path

_STATIC_SCRIPTS = (
    '<script src="/ui/runtime/exploitstrapVersionPicker.js?v=6"></script>'
    '<script src="/ui/runtime/executorCompatibility.js?v=7"></script>'
    '<script src="/ui/runtime/gamesManager.js?v=8"></script>'
    '<script src="/ui/runtime/customUi.js?v=6"></script>'
    '<script src="/ui/runtime/appUpdate.js?v=1"></script>'
)



def _load_html_ui() -> str:
    with open(resource_path("ui", "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    if "</body>" in html:
        return html.replace("</body>", _STATIC_SCRIPTS + "</body>", 1)
    return html + _STATIC_SCRIPTS


# Active dashboard shell. CSS and JavaScript live under ui/ and are served by FastAPI.
HTML_UI = _load_html_ui()


def get_html_ui() -> str:
    """Fresh HTML per call so edits to ui/index.html show up without a restart."""
    try:
        return _load_html_ui()
    except Exception:
        return HTML_UI
