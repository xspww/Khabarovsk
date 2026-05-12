from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional


_ERROR_CODE_RE = re.compile(r"error\s*code[:\s]+(\d+)", re.IGNORECASE)
_DISCONNECT_KEYWORDS = (
    "disconnected",
    "connection attempt failed",
    "lost connection",
    "please rejoin",
    "same account launched",
    "moderation message",
)


def classify_log_line(line: Any) -> Dict[str, Any]:
    text = str(line or "")
    lower = text.lower()
    code_match = _ERROR_CODE_RE.search(text)
    code = str(code_match.group(1) or "") if code_match else ""
    keyword = next((item for item in _DISCONNECT_KEYWORDS if item in lower), "")
    confidence = 0.0
    if code:
        confidence += 0.8
    if keyword:
        confidence += 0.4
    return {
        "matched": bool(confidence >= 0.8),
        "source": "roblox_log",
        "error_code": code,
        "keyword": keyword,
        "confidence": round(confidence, 2),
        "line": text[-240:],
    }


def default_log_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "logs"


def collect_recent_log_evidence(
    log_dir: Optional[Any] = None,
    since_seconds: float = 180.0,
    max_files: int = 4,
    max_lines: int = 240,
) -> Dict[str, Any]:
    root = Path(log_dir) if log_dir is not None else default_log_dir()
    if not root.exists():
        return {"matched": False, "source": "roblox_log", "reason": "log_dir_missing"}

    now = time.time()
    candidates = []
    for path in root.glob("*.log"):
        try:
            modified = float(path.stat().st_mtime)
        except OSError:
            continue
        if (now - modified) <= max(30.0, float(since_seconds or 180.0)):
            candidates.append((modified, path))
    candidates.sort(reverse=True)

    best: Dict[str, Any] = {"matched": False, "source": "roblox_log", "reason": "no_recent_disconnect_evidence"}
    for _modified, path in candidates[: max(1, int(max_files or 1))]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(20, int(max_lines or 20)):]
        except OSError:
            continue
        for line in reversed(lines):
            evidence = classify_log_line(line)
            if evidence.get("confidence", 0.0) > float(best.get("confidence", 0.0) or 0.0):
                best = dict(evidence)
                best["file"] = path.name
            if evidence.get("matched"):
                best = dict(evidence)
                best["file"] = path.name
                return best
    return best
