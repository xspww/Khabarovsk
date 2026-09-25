"""Project-owned signatures for release assets.

These signatures authenticate update packages without requiring a paid
Windows publisher certificate. The public key is part of the launcher;
the matching private key is stored only as a GitHub Actions secret.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

RELEASE_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAqD/2EGTNP3hNk8r7tCI6
BJrSmzjDrPAorMkxoSPAVWDZwYKXLTUe6YoEHAu2zTtDRqIOsQAuj3ZFcSeDjCRQ
F4BMoJabyE9A18i1id56SYq42MifH6LAivfV2sVtsfAGT2NJALsdi2gBlkwOcrT/
26NmvrN7UBdKnnlzsF522AsMJhj9WNwpeXoeqvycqasFif0neljeGlMeqWrKCVTG
KCAdLtmTmAIxeQGbP155so0ZQ+BJRmyaP25xn0MHlLfRApvmL4yc5qwshBr9YifC
uzfre8sTOLAtDQ1m0TvWe06xGEF5heC9GiYuEtyyKfThkRxaaPBaeF8caRUXo3NO
JP/zBrxn+ZvveYObXWScJ4LYcnovzzHyU7upkIergJfUJpq7zi6brapTpphv3nEm
FUb7H3JxCGfIwMgzZ2Jw958v/lu6/D2nfJbp1yLtWg3mgCbvpsd0IR+ITAe1+0Ws
Yxdt65k3hPinWUN43n+1eCjgfvcFPhsdoFrU2ESmkGFJAgMBAAE=
-----END PUBLIC KEY-----
"""

_MAX_SIGNATURE_MANIFEST_BYTES = 16 * 1024
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _digest_file(path: str | os.PathLike[str]) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _signed_message(version: str, filename: str, digest_hex: str) -> bytes:
    if not _VERSION_RE.fullmatch(version):
        raise ValueError("invalid release version")
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename or "\x00" in filename:
        raise ValueError("invalid release filename")
    if not re.fullmatch(r"[0-9a-f]{64}", digest_hex):
        raise ValueError("invalid release SHA-256")
    return b"CronusLauncher release v1\x00" + version.encode("ascii") + b"\x00" + filename.encode("utf-8") + b"\x00" + digest_hex.encode("ascii")


def create_signature_manifest(path: str, version: str, private_key_pem: bytes) -> bytes:
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 3072:
        raise ValueError("release signing key must be RSA 3072-bit or stronger")
    public_key = serialization.load_pem_public_key(RELEASE_PUBLIC_KEY_PEM)
    if (not isinstance(public_key, rsa.RSAPublicKey)
            or private_key.public_key().public_numbers() != public_key.public_numbers()):
        raise ValueError("configured private key does not match the launcher's pinned release key")
    filename = Path(path).name
    digest = _digest_file(path)
    digest_hex = digest.hex()
    message = _signed_message(version, filename, digest_hex)
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return json.dumps({
        "schema": 1,
        "product": "CronusLauncher",
        "version": version,
        "filename": filename,
        "sha256": digest_hex,
        "signature": base64.b64encode(signature).decode("ascii"),
    }, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"


def verify_signature_manifest(path: str, manifest_data: bytes, version: str, filename: str) -> bool:
    if not os.path.isfile(path) or len(manifest_data) > _MAX_SIGNATURE_MANIFEST_BYTES:
        return False
    try:
        manifest: Dict[str, Any] = json.loads(manifest_data.decode("ascii"))
        if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest["schema"] != 1:
            return False
        if manifest.get("product") != "CronusLauncher":
            return False
        if manifest.get("version") != version or manifest.get("filename") != filename:
            return False
        digest_hex = manifest.get("sha256")
        if not isinstance(digest_hex, str) or _digest_file(path).hex() != digest_hex:
            return False
        message = _signed_message(version, filename, digest_hex)
        signature = base64.b64decode(manifest.get("signature", ""), validate=True)
        public_key = serialization.load_pem_public_key(RELEASE_PUBLIC_KEY_PEM)
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 3072:
            return False
        public_key.verify(
            signature,
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        return False


def _sign_cli() -> int:
    parser = argparse.ArgumentParser(description="Sign a Cronus release asset")
    parser.add_argument("--file", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    encoded_key = os.environ.get("CRONUS_RELEASE_SIGNING_PRIVATE_KEY_B64", "")
    if not encoded_key:
        parser.error("CRONUS_RELEASE_SIGNING_PRIVATE_KEY_B64 is not configured")
    try:
        private_key_pem = base64.b64decode(encoded_key, validate=True)
        manifest = create_signature_manifest(args.file, args.version, private_key_pem)
        Path(args.output).write_bytes(manifest)
        return 0
    except Exception as exc:
        print(f"release signing failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_sign_cli())
