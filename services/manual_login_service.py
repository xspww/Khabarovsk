"""Manual Login sessions (Add Accounts -> Manual Login -> Single/Multi).

Ports the RBX Alt Manager flow to this launcher:

* Single: open one popup browser on https://www.roblox.com/login. Optional
  credentials are prefilled (username/password fields + login click, like the
  reference ``LoginTask``); 2FA, captcha and passkey are completed by the
  user in the popup. The ``.ROBLOSECURITY`` cookie is captured automatically
  and imported through :meth:`AccountDataStore.import_cookie_lines`, so ban /
  locked / recovery / captcha marks behave exactly like Cookie Import.
* Multi: paste ``user:pass`` per line. Popups open one after another, each
  prefilled with its own credentials, sequentially like the reference
  ``bulkUserPass`` flow.

The popup browser is Playwright Chromium (the reference uses Puppeteer
Chromium the same way: downloaded on first use, isolated profile per login,
never touches the user's own browser). Passwords are transient only: they
are passed to the login page, never stored, never logged, never returned by
any status API, and wiped from the session entry once used.
"""

from __future__ import annotations

import inspect
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple

LOGIN_URL = "https://www.roblox.com/login"
LOGIN_USERNAME_SELECTOR = "#login-username"
LOGIN_PASSWORD_SELECTOR = "#login-password"
LOGIN_BUTTON_SELECTOR = "#login-button"

ROBLOSECURITY_COOKIE = ".ROBLOSECURITY"
ROBLOX_COOKIE_DOMAIN = ".roblox.com"

# One popup at a time per session; per-account wait so 2FA/passkey has room.
DEFAULT_LOGIN_TIMEOUT_S = 600.0
LOGIN_POLL_INTERVAL_S = 1.0
MAX_MULTI_ENTRIES = 50

# Mirrors the reference Page_RequestFinished hook: a successful login posts to
# these auth endpoints, so a cookie read right after one is almost instant.
LOGIN_SUCCESS_PATHS = (
    "/v2/login",
    "/v2/signup",
)
TWO_STEP_RE = re.compile(r"/users/\d+/two-step-verification/login", re.IGNORECASE)

PLAYWRIGHT_INSTALL_HINT = (
    "Manual Login needs a login browser. Install Google Chrome or Edge, or run: "
    "python -m pip install playwright && python -m playwright install chromium"
)

# Shown when the prefilled user:pass is rejected (UI polls `detail` per entry).
BAD_PASSWORD_DETAIL = "รหัสผิด (incorrect username or password) — skipped"
# Shown when the account is banned/confirmed-deleted: no popup can help.
BANNED_DETAIL = "โดนแบน (account banned by Roblox) — skipped"
# Shown while the popup stays open so the user can finish the step manually.
LOCKED_DETAIL = "ติด Locked (ต้องยืนยันตัวตน) — ทำต่อใน popup..."
RECOVERY_DETAIL = "ติด Recovery (ต้องทำ 2FA/passkey) — ทำต่อใน popup..."
CAPTCHA_DETAIL = "ติด Captcha — แก้ใน popup..."

# Substrings (lowercase) of the Roblox login-page error banner that mean the
# prefilled credentials were rejected. The popup prefills user+pass and clicks
# Log In, so without this the queue hangs on "Waiting for sign-in..." until
# the 10-minute timeout even though the page already says the password is
# wrong (see "An unknown error occurred. Please try again." after a bad pass).
WRONG_PASSWORD_PAGE_PHRASES = (
    "incorrect username or password",
    "incorrect password",
    "wrong password",
    "invalid username or password",
    "invalid password",
    "unknown error occurred",
    "user does not exist",
    "username doesn't exist",
    "username does not exist",
)
# How many consecutive polls (~1s each) the error banner must stay visible
# before the popup is closed and the queue moves on. Gives the user time to
# fix the password manually in the popup; persistent error => auto-skip.
WRONG_PASSWORD_SKIP_AFTER_POLLS = 15


class ManualLoginUnavailable(RuntimeError):
    """Playwright (pip package) is not installed."""


class ManualLoginAborted(RuntimeError):
    """The user closed the popup before signing in."""


class ManualLoginBadCredentials(RuntimeError):
    """Prefilled username/password rejected by Roblox (wrong password)."""


class ManualLoginRejected(RuntimeError):
    """Account unusable for login (e.g. banned): fail + skip, no popup."""


class ManualLoginSkipped(RuntimeError):
    """The user pressed Skip for the current entry."""


def parse_userpass_entries(text: str) -> list[dict[str, str]]:
    """Parse ``user:pass`` per line for Manual Login - Multi.

    Blank lines and lines without ``:`` are skipped. The password keeps
    everything after the first ``:`` (passwords may contain ``:``).
    """
    entries: list[dict[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        username, password = line.split(":", 1)
        username = username.strip()
        password = password.strip()
        if not username:
            continue
        entries.append({"username": username, "password": password})
    return entries


@dataclass
class ManualLoginEntry:
    username_hint: str = ""
    status: str = "pending"  # pending|active|done|failed|cancelled
    detail: str = ""
    imported_as: str = ""
    password: str = field(default="", repr=False)

    def public(self) -> dict[str, str]:
        return {
            "username": self.username_hint or self.imported_as,
            "status": self.status,
            "detail": self.detail,
            "imported_as": self.imported_as,
        }


@dataclass
class ManualLoginSession:
    session_id: str
    mode: str  # single|multi
    entries: list[ManualLoginEntry] = field(default_factory=list)
    state: str = "running"  # running|installing|done|cancelled|error
    note: str = ""
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    skip_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def public(self) -> dict[str, Any]:
        with self.lock:
            items = [entry.public() for entry in self.entries]
            done = sum(1 for entry in self.entries if entry.status == "done")
            failed = sum(1 for entry in self.entries if entry.status == "failed")
            return {
                "session_id": self.session_id,
                "mode": self.mode,
                "state": self.state,
                "note": self.note,
                "total": len(items),
                "done": done,
                "failed": failed,
                "entries": items,
                "created_at": self.created_at,
            }


def _redact_line_for_audit(username: str) -> str:
    return str(username or "").strip() or "unknown"


class _CredentialOutcomes(NamedTuple):
    bad: str
    banned: str
    captcha: str
    locked: str
    recovery: str
    transient: str
    valid: str


def _credential_outcomes() -> _CredentialOutcomes:
    try:
        from services.credential_check import (
            BAD_CREDENTIALS,
            BANNED,
            CAPTCHA,
            LOCKED,
            RECOVERY,
            TRANSIENT,
            VALID,
        )

        return _CredentialOutcomes(BAD_CREDENTIALS, BANNED, CAPTCHA, LOCKED, RECOVERY, TRANSIENT, VALID)
    except Exception:
        return _CredentialOutcomes("bad_credentials", "banned", "captcha", "locked", "recovery", "transient", "valid")


def _unknown_credentials() -> dict[str, str]:
    return {"outcome": "unknown", "detail": "", "fresh_cookie": ""}


def _credential_result(result: Any) -> dict[str, str]:
    if isinstance(result, dict) and result.get("outcome"):
        return {
            "outcome": str(result.get("outcome") or "unknown"),
            "detail": str(result.get("detail") or ""),
            "fresh_cookie": str(result.get("fresh_cookie") or ""),
        }
    return {}


def _precheck_failure(outcome: str, constants: _CredentialOutcomes) -> None:
    if outcome == constants.bad:
        raise ManualLoginBadCredentials(BAD_PASSWORD_DETAIL)
    if outcome == constants.banned:
        raise ManualLoginRejected(BANNED_DETAIL)


def _precheck_note(outcome: str, constants: _CredentialOutcomes) -> tuple[str, str]:
    if outcome == constants.locked:
        return "", LOCKED_DETAIL
    if outcome == constants.recovery:
        return "", RECOVERY_DETAIL
    if outcome == constants.captcha:
        return "", CAPTCHA_DETAIL
    return "", ""


def _fresh_cookie_result(result: dict[str, str], entry: ManualLoginEntry) -> tuple[str, str]:
    fresh = str(result.get("fresh_cookie") or "").strip()
    if fresh:
        entry.password = ""
        return fresh, ""
    return "", ""


def _response_marks_login(response: Any, login_seen: threading.Event) -> None:
    try:
        url = str(getattr(response, "url", "") or "")
        method = str(getattr(getattr(response, "request", None), "method", "") or "").upper()
    except Exception:
        return
    if method != "POST":
        return
    low = url.lower()
    if any(path in low for path in LOGIN_SUCCESS_PATHS) or TWO_STEP_RE.search(low):
        login_seen.set()


def _base_login_note(status_note: str) -> str:
    return str(status_note or "").strip() or "Waiting for sign-in in the popup browser (2FA / captcha / passkey OK)..."


def _wrong_password_note(page_error: str, polls: int) -> str:
    return (
        f"รหัสผิด? Roblox says: {page_error} "
        f"— fix it in the popup or it auto-skips "
        f"({polls}/{WRONG_PASSWORD_SKIP_AFTER_POLLS})..."
    )


def _notify_note(on_note: Callable[[str], None], text: str) -> None:
    try:
        on_note(text)
    except Exception:
        "intentional: ignore exception"


def _exc_text(exc: BaseException, fallback: str) -> str:
    return str(exc) or fallback


def _check_wait_cancelled(cancel_event: threading.Event | None, skip_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Cancelled.")
    if skip_event is not None and skip_event.is_set():
        try:
            skip_event.clear()
        except Exception:
            "intentional: ignore exception"
        raise ManualLoginSkipped("Skipped by user.")


def _check_popup_open(page: Any) -> None:
    try:
        if page.is_closed():
            raise ManualLoginAborted("Popup closed before sign-in completed.")
    except ManualLoginAborted:
        raise
    except Exception:
        "intentional: ignore exception"


class ManualLoginManager:
    """Owns Manual Login sessions and their background worker threads."""

    def __init__(
        self,
        store: Any = None,
        validator: Callable[[str], Any] | None = None,
        on_imported: Callable[[], None] | None = None,
        audit: Callable[..., None] | None = None,
        browser_login: Callable[..., str] | None = None,
        login_timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
        credential_check: Callable[..., Any] | None = None,
    ) -> None:
        self._store = store
        self._validator = validator
        self._on_imported = on_imported
        self._audit = audit
        self._browser_login = browser_login or self._playwright_login
        self._credential_check = credential_check
        self._login_timeout_s = float(login_timeout_s or DEFAULT_LOGIN_TIMEOUT_S)
        self._sessions: dict[str, ManualLoginSession] = {}
        self._guard = threading.Lock()

    # -- store / validator lazy binding (avoids import cycles at module load) --
    @property
    def store(self) -> Any:
        if self._store is None:
            from services.account_hybrid import ACCOUNT_STORE

            self._store = ACCOUNT_STORE
        return self._store

    @property
    def validator(self) -> Callable[[str], Any]:
        if self._validator is None:
            from services.roblox_validation import validate_cookie_details as _validate

            self._validator = _validate
        return self._validator

    def _emit_audit(self, action: str, username: str = "", ok: bool = True, **fields: Any) -> None:
        try:
            if self._audit is not None:
                self._audit(action, username=username, ok=ok, **fields)
            else:
                from services.account_hybrid import audit_event as _audit_event

                _audit_event(action, username=username, ok=ok, **fields)
        except Exception:
            "intentional: ignore exception"

    # -- public API ------------------------------------------------------
    def start_single(self, username: str = "", password: str = "") -> ManualLoginSession:
        session = ManualLoginSession(
            session_id=secrets.token_hex(8),
            mode="single",
            entries=[ManualLoginEntry(username_hint=str(username or "").strip(), password=str(password or ""))],
        )
        return self._launch(session)

    def start_multi(self, entries: list[dict[str, str]]) -> ManualLoginSession:
        clean = [
            ManualLoginEntry(username_hint=str(item.get("username") or "").strip(), password=str(item.get("password") or ""))
            for item in entries or []
            if str(item.get("username") or "").strip()
        ][:MAX_MULTI_ENTRIES]
        if not clean:
            raise ValueError("No valid user:pass lines (need username:password per line)")
        session = ManualLoginSession(session_id=secrets.token_hex(8), mode="multi", entries=clean)
        return self._launch(session)

    def get_session(self, session_id: str) -> ManualLoginSession | None:
        with self._guard:
            return self._sessions.get(str(session_id or ""))

    def public_status(self, session_id: str) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        return session.public() if session else None

    def cancel(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        if session is None:
            return False
        session.cancel_event.set()
        with session.lock:
            if session.state in {"running", "installing"}:
                session.state = "cancelled"
                session.note = "Cancelled. The popup can be closed."
            for entry in session.entries:
                if entry.status in {"pending", "active"}:
                    entry.status = "cancelled"
                    entry.detail = entry.detail or "cancelled"
        self._emit_audit("manual_login_cancel", username="", ok=True, mode=session.mode)
        return True

    def skip_current(self, session_id: str) -> bool:
        """Skip the currently-active entry; the queue moves to the next one."""
        session = self.get_session(session_id)
        if session is None:
            return False
        session.skip_event.set()
        self._emit_audit("manual_login_skip", username="", ok=True, mode=session.mode)
        return True

    # -- internals --------------------------------------------------------
    def _launch(self, session: ManualLoginSession) -> ManualLoginSession:
        with self._guard:
            self._sessions[session.session_id] = session
            while len(self._sessions) > 20:
                oldest = min(self._sessions.values(), key=lambda s: s.created_at)
                if oldest.session_id == session.session_id:
                    break
                self._sessions.pop(oldest.session_id, None)
        self._emit_audit("manual_login_start", username="", ok=True, mode=session.mode, total=len(session.entries))
        worker = threading.Thread(target=self._run_session, args=(session,), name=f"ManualLogin-{session.session_id}", daemon=True)
        worker.start()
        return session

    def _set_state(self, session: ManualLoginSession, state: str, note: str = "") -> None:
        with session.lock:
            session.state = state
            if note:
                session.note = note

    def _run_session(self, session: ManualLoginSession) -> None:
        precheck_blocked = False  # set on rate-limit/transient: skip further API prechecks
        try:
            for entry in session.entries:
                if session.cancel_event.is_set():
                    self._cancel_pending_entry(session, entry)
                    continue
                with session.lock:
                    entry.status = "active"
                    entry.detail = "Waiting for sign-in in the popup browser..."
                session.skip_event.clear()
                keep_going, precheck_blocked = self._attempt_login(session, entry, precheck_blocked)
                if not keep_going:
                    break
            self._finish_session(session)
        except Exception as exc:
            self._set_state(session, "error", str(exc) or "manual login failed")
            self._emit_audit("manual_login_error", username="", ok=False, detail=str(exc)[:200])

    def _cancel_pending_entry(self, session: ManualLoginSession, entry: ManualLoginEntry) -> None:
        with session.lock:
            if entry.status == "pending":
                entry.status = "cancelled"
                entry.detail = "cancelled"

    def _fail_entry(
        self,
        session: ManualLoginSession,
        entry: ManualLoginEntry,
        detail: str,
        status: str = "failed",
    ) -> None:
        with session.lock:
            entry.status = status
            entry.detail = detail

    def _attempt_login(
        self,
        session: ManualLoginSession,
        entry: ManualLoginEntry,
        precheck_blocked: bool,
    ) -> tuple[bool, bool]:
        """Run one entry login attempt; returns (keep_going, precheck_blocked)."""
        try:
            # Fast path: when user:pass was pasted, ask Roblox directly
            # first. Wrong passwords / banned accounts are marked failed
            # + skipped without a popup; locked / recovery / captcha
            # notes are shown while the popup stays open for the user
            # to finish the step manually.
            precheck_cookie, status_note = self._precheck_credentials(
                session, entry, blocked=precheck_blocked,
            )
            if precheck_cookie == "__BLOCKED__":
                precheck_blocked = True
                precheck_cookie = ""
            if precheck_cookie:
                self._import_captured_cookie(session, entry, precheck_cookie)
                return True, precheck_blocked
            cookie = self._call_browser_login(
                session,
                entry,
                status_note=status_note,
            )
        except ManualLoginUnavailable as exc:
            self._fail_entry(session, entry, _exc_text(exc, PLAYWRIGHT_INSTALL_HINT))
            self._set_state(session, "error", PLAYWRIGHT_INSTALL_HINT)
            self._emit_audit(
                "manual_login_unavailable",
                username=_redact_line_for_audit(entry.username_hint),
                ok=False,
                mode=session.mode,
            )
            return False, precheck_blocked
        except (ManualLoginBadCredentials, ManualLoginRejected) as exc:
            self._fail_entry(session, entry, _exc_text(exc, BAD_PASSWORD_DETAIL))
            self._emit_audit(
                "manual_login_rejected",
                username=_redact_line_for_audit(entry.username_hint),
                ok=False,
                mode=session.mode,
                detail=str(exc)[:200],
            )
            return True, precheck_blocked
        except ManualLoginSkipped as exc:
            self._fail_entry(session, entry, _exc_text(exc, "Skipped by user."))
            self._emit_audit(
                "manual_login_skipped",
                username=_redact_line_for_audit(entry.username_hint),
                ok=False,
                mode=session.mode,
            )
            return True, precheck_blocked
        except Exception as exc:
            status = "failed" if not session.cancel_event.is_set() else "cancelled"
            self._fail_entry(session, entry, _exc_text(exc, "login failed"), status)
            self._emit_audit(
                "manual_login_failed",
                username=_redact_line_for_audit(entry.username_hint),
                ok=False,
                mode=session.mode,
                detail=str(exc)[:200],
            )
            return True, precheck_blocked
        finally:
            # Passwords are transient: never kept after the attempt.
            entry.password = ""
        self._import_captured_cookie(session, entry, cookie)
        return True, precheck_blocked

    def _finish_session(self, session: ManualLoginSession) -> None:
        with session.lock:
            if session.state not in {"running", "installing"}:
                return
            if session.cancel_event.is_set():
                session.state = "cancelled"
            elif any(entry.status == "failed" for entry in session.entries):
                session.state = "done"
                session.note = session.note or "Finished with failures."
            else:
                session.state = "done"
                session.note = session.note or "All logins captured."

    def _set_entry_note(self, session: ManualLoginSession, entry: ManualLoginEntry, text: str) -> None:
        with session.lock:
            if entry.status == "active" and text:
                entry.detail = text

    def _call_browser_login(
        self,
        session: ManualLoginSession,
        entry: ManualLoginEntry,
        status_note: str = "",
    ) -> str:
        """Call the injected (or real) browser login, tolerating older fakes.

        Extra kwargs (``skip_event`` / ``status_note``) are dropped when the
        callable does not accept them, so test doubles with a minimal
        signature keep working.
        """
        full_kwargs: dict[str, Any] = {
            "username_hint": entry.username_hint,
            "password": entry.password,
            "timeout_s": self._login_timeout_s,
            "cancel_event": session.cancel_event,
            "skip_event": session.skip_event,
            "status_note": status_note,
            "on_note": lambda text: self._set_entry_note(session, entry, text),
        }
        try:
            params = inspect.signature(self._browser_login).parameters
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                full_kwargs = {k: v for k, v in full_kwargs.items() if k in params}
        except Exception:
            "intentional: ignore exception"
        return self._browser_login(**full_kwargs)

    def _classify_pasted_credentials(
        self,
        username: str,
        password: str,
    ) -> dict[str, str]:
        """One login attempt via the Auth API. Never raises, never logs secrets.

        Returns {"outcome": ..., "detail": ..., "fresh_cookie": ...}.
        Falls back to {"outcome": "unknown"} when the checker is unavailable.
        """
        if self._credential_check is not None:
            try:
                result = self._credential_check(username, password, timeout=12.0)
            except Exception:
                return _unknown_credentials()
            return _credential_result(result) or _unknown_credentials()
        try:
            from services.credential_check import check_credentials as _check
        except Exception:
            return _unknown_credentials()
        try:
            result = _check(username, password, timeout=12.0)
        except Exception:
            return _unknown_credentials()
        return _credential_result(result) or _unknown_credentials()

    def _precheck_credentials(
        self,
        session: ManualLoginSession,
        entry: ManualLoginEntry,
        blocked: bool = False,
    ) -> tuple:
        """Ask Roblox directly when user:pass was pasted.

        Returns (cookie, status_note):
        * (fresh_cookie, "") — password login already succeeded, import it, no popup.
        * ("", note) — continue with the popup browser, showing `note` if set
          (locked / recovery / captcha: the user finishes the step in the popup).
        * ("__BLOCKED__", "") — rate-limited (caller disables further prechecks).
        Raises ManualLoginBadCredentials (wrong password) or ManualLoginRejected
        (banned) so the entry is marked failed + skipped without a popup.
        Passwords stay transient: never logged, never stored.
        """
        username = str(entry.username_hint or "").strip()
        password = str(entry.password or "")
        if not username or not password or blocked:
            return "", ""
        self._set_entry_note(session, entry, "Checking username/password...")
        constants = _credential_outcomes()
        result = self._classify_pasted_credentials(username, password)
        outcome = str(result.get("outcome") or "unknown")
        _precheck_failure(outcome, constants)
        if outcome == constants.transient:
            return "__BLOCKED__", ""
        if outcome == constants.valid:
            return _fresh_cookie_result(result, entry)
        return _precheck_note(outcome, constants)

    def _import_captured_cookie(self, session: ManualLoginSession, entry: ManualLoginEntry, cookie: str) -> None:
        cookie = str(cookie or "").strip()
        hint = _redact_line_for_audit(entry.username_hint)
        if not cookie:
            self._fail_entry(session, entry, "No cookie captured.")
            return
        line = f"{entry.username_hint}:{cookie}" if entry.username_hint else cookie
        try:
            result = self.store.import_cookie_lines([line], validator=self.validator)
        except Exception as exc:
            self._fail_entry(session, entry, f"Import failed: {exc}")
            self._emit_audit("manual_login_import_failed", username=hint, ok=False, mode=session.mode, detail=str(exc)[:200])
            return
        errors = [str(e) for e in (result.get("errors") or []) if str(e).strip()]
        imported = int(result.get("imported") or 0)
        self._apply_import_result(session, entry, hint, errors, imported)
        self._emit_audit(
            "manual_login_import",
            username=hint,
            ok=imported > 0,
            mode=session.mode,
            detail=(errors[0] if errors else "imported")[:200],
        )
        if imported > 0 and self._on_imported is not None:
            try:
                self._on_imported()
            except Exception:
                "intentional: ignore exception"

    def _apply_import_result(
        self,
        session: ManualLoginSession,
        entry: ManualLoginEntry,
        hint: str,
        errors: list[str],
        imported: int,
    ) -> None:
        with session.lock:
            if imported > 0:
                entry.status = "done"
                entry.imported_as = hint
                entry.detail = errors[0] if errors else "Imported."
            else:
                entry.status = "failed"
                entry.detail = errors[0] if errors else "Cookie rejected."

    # -- Playwright popup browser -----------------------------------------
    def _playwright_login(
        self,
        username_hint: str = "",
        password: str = "",
        timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
        cancel_event: threading.Event | None = None,
        skip_event: threading.Event | None = None,
        status_note: str = "",
        on_note: Callable[[str], None] | None = None,
    ) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            raise ManualLoginUnavailable(PLAYWRIGHT_INSTALL_HINT)
        deadline = time.time() + max(30.0, float(timeout_s or DEFAULT_LOGIN_TIMEOUT_S))
        with sync_playwright() as playwright:
            browser = self._launch_chromium(playwright, on_note=on_note)
            try:
                context = browser.new_context(viewport={"width": 880, "height": 740})
                page = context.new_page()
                login_seen = threading.Event()
                try:
                    page.on("response", lambda response: _response_marks_login(response, login_seen))
                except Exception:
                    "intentional: ignore exception"
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                self._prefill_login_page(page, username_hint, password)
                password = ""
                cookie = self._wait_for_roblosecurity(
                    context, page, login_seen, deadline, cancel_event,
                    skip_event=skip_event, status_note=status_note, on_note=on_note,
                )
                return cookie
            finally:
                try:
                    browser.close()
                except Exception:
                    "intentional: ignore exception"

    def _launch_chromium(self, playwright: Any, on_note: Callable[[str], None] | None = None) -> Any:
        launch_args = ["--disable-web-security"]
        # Prefer the browsers already on the machine (no download, isolated
        # throwaway profile per login). Fall back to bundled Chromium.
        for channel in ("chrome", "msedge"):
            try:
                return playwright.chromium.launch(headless=False, channel=channel, args=launch_args)
            except Exception:
                continue
        try:
            return playwright.chromium.launch(headless=False, args=launch_args)
        except Exception as exc:
            message = str(exc)
            if "Executable doesn't exist" not in message and "executable" not in message.lower():
                raise
        if getattr(sys, "frozen", False):
            # Frozen exe has no `python -m playwright` entry point. Chrome /
            # Edge cover every Windows 10/11 machine, so reaching here means
            # neither is installed.
            raise ManualLoginUnavailable(
                "No login browser found. Install Google Chrome or Microsoft Edge, then retry Manual Login."
            )
        if on_note is not None:
            try:
                on_note("First run: downloading Chromium for the login popup...")
            except Exception:
                "intentional: ignore exception"
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
            )
        except Exception as exc:
            raise ManualLoginUnavailable(f"Chromium download failed: {exc}. {PLAYWRIGHT_INSTALL_HINT}")
        return playwright.chromium.launch(headless=False, args=launch_args)

    @staticmethod
    def _prefill_login_page(page: Any, username: str, password: str) -> None:
        """Fill Roblox login fields like the reference LoginTask (best effort)."""
        try:
            page.evaluate("document.body.classList.remove('light-theme');document.body.classList.add('dark-theme');")
        except Exception:
            "intentional: ignore exception"
        try:
            if username:
                page.wait_for_selector(LOGIN_USERNAME_SELECTOR, timeout=5000)
                page.fill(LOGIN_USERNAME_SELECTOR, username)
            if password:
                page.wait_for_selector(LOGIN_PASSWORD_SELECTOR, timeout=5000)
                page.fill(LOGIN_PASSWORD_SELECTOR, password)
            if username and password:
                page.click(LOGIN_BUTTON_SELECTOR)
        except Exception:
            "intentional: ignore exception"

    @staticmethod
    def _roblosecurity_value(item: Any) -> str:
        try:
            name = str(item.get("name") or "")
            domain = str(item.get("domain") or "")
            value = str(item.get("value") or "")
        except Exception:
            return ""
        if name == ROBLOSECURITY_COOKIE and ROBLOX_COOKIE_DOMAIN in domain and value.startswith("_|WARNING:"):
            return value
        return ""

    @staticmethod
    def _read_roblosecurity(context: Any) -> str:
        try:
            cookies = context.cookies()
        except Exception:
            return ""
        for item in cookies or []:
            value = ManualLoginManager._roblosecurity_value(item)
            if value:
                return value
        return ""

    @staticmethod
    def _read_login_error(page: Any) -> str:
        """Best-effort read of the login-page error banner (wrong password).

        Returns the banner text when it matches a wrong-password phrase,
        else "". Never raises: the wait loop must survive any DOM state.
        """
        try:
            text = str(page.evaluate(
                """(() => {
                    const pick = (sel) => {
                        try {
                            const el = document.querySelector(sel);
                            return el ? (el.innerText || el.textContent || '') : '';
                        } catch (e) { return ''; }
                    };
                    const direct = [
                        '#login-form-error', '#login-error',
                        '[data-testid="login-error"]',
                        '.login-error', '.text-error', '.alert-error',
                        '.error-message', '.form-error',
                    ].map(pick).filter(Boolean).join(' ').trim();
                    if (direct) return direct.slice(0, 300);
                    try {
                        const body = document.body ? (document.body.innerText || '') : '';
                        return body.slice(0, 2000);
                    } catch (e) { return ''; }
                })()"""
            ) or "")
        except Exception:
            return ""
        lowered = text.lower()
        for phrase in WRONG_PASSWORD_PAGE_PHRASES:
            if phrase in lowered:
                # Return the matched sentence for the detail line.
                for line in text.splitlines():
                    if phrase in line.lower() and line.strip():
                        return line.strip()[:200]
                return phrase
        return ""

    def _wait_for_roblosecurity(
        self,
        context: Any,
        page: Any,
        login_seen: threading.Event,
        deadline: float,
        cancel_event: threading.Event | None,
        skip_event: threading.Event | None = None,
        status_note: str = "",
        on_note: Callable[[str], None] | None = None,
    ) -> str:
        last_note = ""
        wrong_password_polls = 0
        base_note = _base_login_note(status_note)
        while time.time() < deadline:
            _check_wait_cancelled(cancel_event, skip_event)
            _check_popup_open(page)
            cookie = self._read_roblosecurity(context)
            if cookie:
                return cookie
            cookie = self._poll_login_cookies(context, login_seen)
            if cookie:
                return cookie
            page_error = self._read_login_error(page)
            if page_error:
                wrong_password_polls += 1
                note = _wrong_password_note(page_error, wrong_password_polls)
                if on_note is not None:
                    last_note = note
                    _notify_note(on_note, note)
                if wrong_password_polls >= WRONG_PASSWORD_SKIP_AFTER_POLLS:
                    raise ManualLoginBadCredentials(
                        f"{BAD_PASSWORD_DETAIL} (Roblox: {page_error})"
                    )
                time.sleep(LOGIN_POLL_INTERVAL_S)
                continue
            wrong_password_polls = 0
            if base_note != last_note and on_note is not None:
                last_note = base_note
                _notify_note(on_note, base_note)
            time.sleep(LOGIN_POLL_INTERVAL_S)
        raise TimeoutError("Timed out waiting for sign-in.")

    def _poll_login_cookies(self, context: Any, login_seen: threading.Event) -> str:
        if not login_seen.is_set():
            return ""
        login_seen.clear()
        for _ in range(5):
            cookie = self._read_roblosecurity(context)
            if cookie:
                return cookie
            time.sleep(0.5)
        return ""


MANUAL_LOGIN_MANAGER = ManualLoginManager()
