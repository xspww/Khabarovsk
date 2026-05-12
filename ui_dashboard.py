from __future__ import annotations

from app_paths import resource_path


def _load_html_ui() -> str:
    with open(resource_path("ui", "index.html"), "r", encoding="utf-8") as f:
        return f.read()


# Active dashboard shell. CSS and JavaScript live under ui/ and are served by FastAPI.
HTML_UI = _load_html_ui()
