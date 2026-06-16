"""Tests for the R2 off-site backup helper.

The SigV4 signing is validated against AWS's own published worked example
("GET ListUsers" from the Signature Version 4 documentation) — if our canonical
request / signing chain is wrong, these exact published digests won't match.
"""
import hashlib

import pytest

from backend import offsite_backup as ob


# --- AWS documented example: GET https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08
AWS_ACCESS_KEY = "AKIDEXAMPLE"
AWS_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def test_sigv4_matches_aws_published_example():
    header, signature = ob._authorization(
        method="GET",
        canonical_uri="/",
        query="Action=ListUsers&Version=2010-05-08",
        headers={
            "content-type": "application/x-www-form-urlencoded; charset=utf-8",
            "host": "iam.amazonaws.com",
            "x-amz-date": "20150830T123600Z",
        },
        payload_hash=EMPTY_SHA256,
        access_key=AWS_ACCESS_KEY,
        secret_key=AWS_SECRET_KEY,
        region="us-east-1",
        service="iam",
        amzdate="20150830T123600Z",
        datestamp="20150830",
    )
    # Published in the AWS SigV4 signing-examples documentation.
    assert signature == "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    assert header.startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, Signature="
    )


def test_signing_key_is_deterministic_chain():
    k = ob._signing_key(AWS_SECRET_KEY, "20150830", "us-east-1", "iam")
    assert isinstance(k, bytes) and len(k) == 32
    assert k == ob._signing_key(AWS_SECRET_KEY, "20150830", "us-east-1", "iam")


@pytest.fixture
def _clear_r2(monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PREFIX"):
        monkeypatch.delenv(k, raising=False)


def test_is_configured_requires_all_keys(_clear_r2, monkeypatch):
    assert ob.is_configured() is False
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    assert ob.is_configured() is False          # bucket still missing
    monkeypatch.setenv("R2_BUCKET", "backtick-backups")
    assert ob.is_configured() is True


def test_upload_noop_when_unconfigured(_clear_r2, tmp_path):
    f = tmp_path / "x.db"
    f.write_bytes(b"data")
    assert ob.upload_file(f) is False           # returns False, makes no network call
