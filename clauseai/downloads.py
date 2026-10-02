"""Stateless HMAC-signed download links for generated documents."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import quote

from clauseai.config import get_base_url, get_download_signing_secret


class DownloadTokenError(Exception):
    """Raised when a download token is missing or invalid."""


class DownloadSigningUnavailable(Exception):
    """Raised when production has no download signing secret."""

    def __init__(self) -> None:
        super().__init__("Document downloads are unavailable")


def _canonical_payload(slug: str, fmt: str, answers: dict[str, str]) -> bytes:
    return json.dumps(
        {"answers": answers, "format": fmt, "slug": slug},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _secret() -> bytes:
    try:
        return get_download_signing_secret().encode("utf-8")
    except RuntimeError as exc:
        raise DownloadSigningUnavailable() from exc


def sign_download(*, slug: str, fmt: str, answers: dict[str, str]) -> str:
    """Return a token that re-renders this document on GET."""
    body = (
        base64.urlsafe_b64encode(_canonical_payload(slug, fmt, answers))
        .decode("ascii")
        .rstrip("=")
    )
    signature = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def build_download_url(*, slug: str, fmt: str, answers: dict[str, str]) -> str:
    """Return an absolute URL for a signed document download."""
    token = sign_download(slug=slug, fmt=fmt, answers=answers)
    return f"{get_base_url()}/api/downloads?token={quote(token, safe='')}"


def read_download_token(token: str) -> tuple[str, str, dict[str, str]]:
    """Return slug, format, and answers from a signed download token."""
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise DownloadTokenError("Invalid download token") from exc
    if not body or not signature:
        raise DownloadTokenError("Invalid download token")

    expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise DownloadTokenError("Invalid download token")

    padded = body + ("=" * (-len(body) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (json.JSONDecodeError, ValueError, UnicodeError) as exc:
        raise DownloadTokenError("Invalid download token") from exc

    if not isinstance(payload, dict):
        raise DownloadTokenError("Invalid download token")
    slug = payload.get("slug")
    fmt = payload.get("format")
    answers = payload.get("answers")
    if not isinstance(slug, str) or not slug.strip():
        raise DownloadTokenError("Invalid download token")
    if not isinstance(fmt, str) or not fmt.strip():
        raise DownloadTokenError("Invalid download token")
    if not isinstance(answers, dict):
        raise DownloadTokenError("Invalid download token")

    cleaned: dict[str, str] = {}
    for key, value in answers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise DownloadTokenError("Invalid download token")
        cleaned[key] = value
    return slug, fmt, cleaned
