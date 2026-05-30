from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from enum import Enum
from typing import Any

from app_paths import LOG_DIR, ensure_log_dir, move_app_data_file
from console_activity import emit_structured as emit_console_activity
from console_activity import emit_text as emit_console_text
from services.safe_rotating_log import ProcessSafeRotatingFileHandler


def _move_log_artifacts_to_log_dir() -> None:
    for filename in (
        "account_tools_audit.jsonl",
        "cronus_watchdog.log",
        "cronus_rt1.log",
        "cronus_rt1_events.jsonl",
    ):
        for suffix in ("", ".1", ".2", ".3", ".lock"):
            move_app_data_file(filename + suffix, os.path.join("logs", filename + suffix))
    for filename in os.listdir(os.path.dirname(LOG_DIR)):
        is_log = (
            filename.startswith("cronus_backend_")
        )
        if is_log and filename.endswith((".log", ".jsonl")):
            move_app_data_file(filename, os.path.join("logs", filename))


_move_log_artifacts_to_log_dir()
ensure_log_dir()

LOG_FILE = os.path.join(LOG_DIR, "cronus_rt1.log")
STRUCTURED_LOG_FILE = os.path.join(LOG_DIR, "cronus_rt1_events.jsonl")


_logger = logging.getLogger("cronus_rt1")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _fh = ProcessSafeRotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _logger.addHandler(_fh)

_structured_logger = logging.getLogger("cronus_rt1.structured")
_structured_logger.setLevel(logging.INFO)
_structured_logger.propagate = False
if not _structured_logger.handlers:
    _json_fh = ProcessSafeRotatingFileHandler(STRUCTURED_LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _json_fh.setFormatter(logging.Formatter("%(message)s"))
    _structured_logger.addHandler(_json_fh)

_SENSITIVE_KEYS = re.compile(r"(cookie|password|token|secret|roblosecurity|privateServerLinkCode|linkCode)", re.I)
_COOKIE_RE = re.compile(r"_\|WARNING:.*?(?=\s|$)", re.I)
_LINK_CODE_RE = re.compile(r"((?:privateServerLinkCode|linkCode)=)[^&\s]+", re.I)


def _redact_value(key: str, value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(key, item) for item in value]
    text = str(value)
    if _SENSITIVE_KEYS.search(str(key)):
        return "[REDACTED]"
    text = _COOKIE_RE.sub("[REDACTED_COOKIE]", text)
    text = _LINK_CODE_RE.sub(r"\1[REDACTED]", text)
    return text


def flog_struct(scope: str, event: str, level: str = "info", **fields):
    record = {
        "ts": round(time.time(), 3),
        "level": str(level or "info").lower(),
        "scope": str(scope or "runtime"),
        "event": str(event or ""),
        "thread": threading.current_thread().name,
    }
    for key, value in fields.items():
        record[str(key)] = _redact_value(str(key), value)
    try:
        _structured_logger.info(json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")))
    except Exception as e:
        _logger.warning("structured_log_failed error=%s", e)


def flog(msg: str, level: str = "info"):
    getattr(_logger, level, _logger.info)(msg)
    emit_console_text(msg, level)


def _kv_value(value: Any) -> str:
    if value is None:
        return "none"
    text = str(_redact_value("", value)).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return '""'
    if any(ch.isspace() for ch in text) or "=" in text:
        return '"' + text.replace('"', '\\"') + '"'
    return text


def flog_kv(scope: str, name: str, level: str = "info", **fields):
    flog_struct(scope, name, level, **fields)
    emit_console_activity(scope, name, level, **fields)
    parts = " ".join(f"{key}={_kv_value(_redact_value(key, value))}" for key, value in fields.items())
    suffix = f" {parts}" if parts else ""
    flog(f"[{scope}] {name}{suffix}", level)
