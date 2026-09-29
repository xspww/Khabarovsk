"""One TLS trust configuration for every outbound HTTPS call in the app.

The failure this fixes (reported on user machines, not the build machine):

    Update failed: download failed after 3 tries: <urlopen error
    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable
    to get local issuer certificate (_ssl.c:1006)>

The giveaway is that the same machine DID receive "Update Available". The
version check connected fine; the download of the same GitHub host did
not. The reason is that app_version_check loaded certifi's CA bundle by
hand while every other HTTPS call in the app trusted
``ssl.create_default_context()``. A frozen PyInstaller build ships no
``cert.pem``, so the default trust store is whatever the host provides:
empty on a clean machine, and missing the inspection CA on a machine
behind a TLS-inspecting proxy (corporate proxy or AV web shield). One
shared context removes the split.

``default_context()`` trusts, additively:

1. the OpenSSL/system default verify paths (unchanged behaviour), and on
   Python 3.11+ the Windows root store that ``create_default_context``
   already loads;
2. an operator-provided bundle named by ``SSL_CERT_FILE`` /
   ``SSL_CERT_DIR`` / ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE``;
3. certifi's maintained root bundle (``requirements.txt``, bundled by
   ``cronus_launcher.spec``);
4. the Windows "CA" and "ROOT" stores, exported to PEM.

Step 4 is what makes interception proxies work: "unable to get local
issuer certificate" is the signature of a proxy that re-signs TLS with
its own CA, and that CA lives in the Windows store, not in certifi.

Verification is never weakened. ``verify_mode`` stays CERT_REQUIRED and
hostname checking stays on, so a tampered or unknown issuer is still
rejected. The only change is a wider, non-empty trust store - the same
set the machine's own browser and Windows TLS already honour. If a
deployment must not trust the machine store, delete step 4 below; the
app then behaves exactly like the previous build.
"""

from __future__ import annotations

import base64
import os
import ssl
import threading
import urllib.request
from typing import Any, Optional, Tuple

# Operators export these to point an app at their corporate CA bundle.
_CA_ENV_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
# "CA" holds user-added roots, "ROOT" the preinstalled trusted ones.
_WINDOWS_CERT_STORES = ("CA", "ROOT")

_LOCK = threading.Lock()
_CONTEXT: Optional[ssl.SSLContext] = None
_SOURCES: Tuple[str, ...] = ()


_PEM_HEADER = "-----BEGIN CERTIFICATE-----"


def _der_to_pem(der: bytes) -> str:
    body = base64.b64encode(der).decode("ascii")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return _PEM_HEADER + "\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"


def _entry_to_pem(entry: Any) -> str:
    """Normalise one ssl.enum_certificates() result to PEM.

    The result shape is not stable across CPython versions: 3.11-3.13 hand
    back dicts keyed 'data'/'encoding', while 3.14 hands back
    ``(pfx, name, der)``-style tuples. Accept both instead of silently
    loading an empty store - the first version of this code assumed dicts
    and quietly trusted nothing beyond certifi on 3.14.
    """
    data = None
    if isinstance(entry, dict):
        data = entry.get("data")
    elif isinstance(entry, (tuple, list)):
        for item in entry:
            if isinstance(item, (bytes, bytearray)) and len(item) > 64:
                data = item
                break
            if isinstance(item, str) and _PEM_HEADER in item:
                return item
    if not data:
        return ""
    if isinstance(data, (bytes, bytearray)):
        try:
            return _der_to_pem(bytes(data))
        except Exception:
            return ""
    if isinstance(data, str):
        if _PEM_HEADER in data:
            return data
        try:
            return _der_to_pem(base64.b64decode(data, validate=False))
        except Exception:
            return ""
    return ""


def _windows_store_pem() -> str:
    """Export the Windows root/CA stores as one PEM bundle (best effort)."""
    enum_certificates = getattr(ssl, "enum_certificates", None)
    if enum_certificates is None:
        return ""
    pem_parts = []
    for store in _WINDOWS_CERT_STORES:
        try:
            entries = enum_certificates(store) or []
        except Exception:
            continue
        for entry in entries:
            try:
                pem = _entry_to_pem(entry)
            except Exception:
                continue
            if pem:
                pem_parts.append(pem)
    return "".join(pem_parts)


def _certifi_pem() -> str:
    """certifi's bundle contents, or '' when certifi is unavailable."""
    try:
        import certifi

        with open(certifi.where(), "rb") as handle:
            return handle.read().decode("ascii", errors="replace")
    except Exception:
        return ""


def _count_certs(pem: str) -> int:
    return pem.count(_PEM_HEADER)


def _build() -> Tuple[ssl.SSLContext, Tuple[str, ...]]:
    context = ssl.create_default_context()
    sources = ["system default verify paths"]

    # Operator override first: an explicit choice must not depend on load order.
    for name in _CA_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if not value:
            continue
        try:
            if os.path.isdir(value):
                context.load_verify_locations(capath=value)
            elif os.path.isfile(value):
                context.load_verify_locations(cafile=value)
            else:
                continue
        except Exception:
            continue
        sources.append(f"{name}={value}")

    # Only report a source that actually loaded, so a TLS failure in the UI
    # never names a trust store the context does not hold.
    for label, pem in (("certifi", _certifi_pem()), ("Windows CA store", _windows_store_pem())):
        if not pem:
            continue
        try:
            context.load_verify_locations(cadata=pem)
        except Exception:
            continue
        sources.append(f"{label} ({_count_certs(pem)} certs)")
    return context, tuple(sources)


def default_context() -> ssl.SSLContext:
    """Cached SSLContext every HTTPS call in the app should use."""
    global _CONTEXT, _SOURCES
    context = _CONTEXT
    if context is not None:
        return context
    with _LOCK:
        if _CONTEXT is None:
            built, sources = _build()
            _CONTEXT = built
            _SOURCES = sources
        return _CONTEXT


def trust_sources() -> Tuple[str, ...]:
    """Human-readable list of what default_context() actually loaded."""
    default_context()
    return _SOURCES


def https_handler() -> urllib.request.HTTPSHandler:
    """HTTPSHandler bound to the shared context, for build_opener()."""
    return urllib.request.HTTPSHandler(context=default_context())


def urlopen(request: Any, timeout: Optional[float] = None) -> Any:
    """urllib.request.urlopen with the shared TLS context attached."""
    return urllib.request.urlopen(request, timeout=timeout, context=default_context())


def looks_like_certificate_error(exc: Optional[BaseException]) -> bool:
    """True when exc is a TLS trust failure worth explaining in the UI."""
    if exc is None:
        return False
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    text = str(exc).upper()
    return "CERTIFICATE_VERIFY_FAILED" in text or "SSL:" in text
