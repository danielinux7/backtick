"""Off-site upload of a backup file to Cloudflare R2 (S3-compatible).

The in-process backup (`backup_scheduler`) writes snapshots to the same disk as
the live DB — that survives app/logical corruption but not disk/account loss.
This pushes each snapshot to R2 so a Render-side failure can't take the backups
with it.

Stdlib-only on purpose (no boto3): R2 speaks S3 SigV4, which is a few dozen lines
of hmac/hashlib — cheaper than dragging botocore into the image, and the signing
math is pinned by a unit test against AWS's published example vector.

Inert unless all of R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
R2_BUCKET are set (optional R2_PREFIX namespaces the keys). Set them with
scripts/sync_render_env.py like every other secret.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import urllib.parse
import urllib.request
from pathlib import Path

_ALGORITHM = "AWS4-HMAC-SHA256"
# R2 uses a fixed pseudo-region for SigV4; the real location is the account host.
_REGION = "auto"
_SERVICE = "s3"

_ENV_KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


def is_configured() -> bool:
    """True only when every required R2 var is present — otherwise upload is a no-op."""
    return all(os.environ.get(k) for k in _ENV_KEYS)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def _authorization(
    *, method: str, canonical_uri: str, query: str, headers: dict[str, str],
    payload_hash: str, access_key: str, secret_key: str, region: str, service: str,
    amzdate: str, datestamp: str,
) -> tuple[str, str]:
    """Build the SigV4 Authorization header value. Pure — unit-tested against
    AWS's documented GET-ListUsers example. Returns (header_value, signature)."""
    keys = sorted(headers)
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in keys)
    signed_headers = ";".join(keys)
    canonical_request = "\n".join(
        [method, canonical_uri, query, canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [_ALGORITHM, amzdate, scope, _sha256_hex(canonical_request.encode("utf-8"))]
    )
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    header = (
        f"{_ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return header, signature


def upload_file(path: str | Path, key: str | None = None, timeout: float = 30.0) -> bool:
    """PUT one file to the configured R2 bucket. Returns True on success, False
    when R2 isn't configured. Raises on an HTTP/network error (callers should
    treat a failure as non-fatal — the local backup already succeeded)."""
    if not is_configured():
        return False
    account = os.environ["R2_ACCOUNT_ID"]
    access_key = os.environ["R2_ACCESS_KEY_ID"]
    secret_key = os.environ["R2_SECRET_ACCESS_KEY"]
    bucket = os.environ["R2_BUCKET"]
    prefix = os.environ.get("R2_PREFIX", "").strip("/")

    path = Path(path)
    name = key or path.name
    object_key = f"{prefix}/{name}" if prefix else name
    body = path.read_bytes()
    payload_hash = _sha256_hex(body)

    host = f"{account}.r2.cloudflarestorage.com"
    # Path-style: /<bucket>/<key>. Our keys are [A-Za-z0-9-._/] so quoting is a no-op,
    # but encode defensively (keep '/' as separators).
    canonical_uri = "/" + urllib.parse.quote(f"{bucket}/{object_key}", safe="/~")

    now = dt.datetime.now(dt.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    signed = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
    }
    auth, _ = _authorization(
        method="PUT", canonical_uri=canonical_uri, query="", headers=signed,
        payload_hash=payload_hash, access_key=access_key, secret_key=secret_key,
        region=_REGION, service=_SERVICE, amzdate=amzdate, datestamp=datestamp,
    )

    req = urllib.request.Request(f"https://{host}{canonical_uri}", data=body, method="PUT")
    req.add_header("Authorization", auth)
    req.add_header("x-amz-content-sha256", payload_hash)
    req.add_header("x-amz-date", amzdate)
    req.add_header("Content-Type", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return 200 <= resp.status < 300
