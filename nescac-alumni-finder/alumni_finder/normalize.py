"""Text, name, URL and domain normalization used for matching and de-duplication."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "cfa", "cpa", "mba", "phd", "md", "jd", "esq", "caia", "frm"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def norm_text(text: str) -> str:
    """Lowercase, strip accents and punctuation (keeping '&'), collapse spaces."""
    text = ascii_fold(text or "").lower()
    text = re.sub(r"[^a-z0-9&]+", " ", text)
    return " ".join(text.split())


def _name_tokens(text: str) -> list[str]:
    text = re.sub(r"\([^)]*\)", " ", text or "")  # drop nicknames / maiden names in parens
    tokens = norm_text(text.replace("&", " ")).split()
    return [t for t in tokens if t not in _NAME_SUFFIXES]


def split_name(full_name: str) -> tuple[str, str]:
    """Best-effort first/last split: 'Jane Q. Doe, CFA' -> ('Jane', 'Doe')."""
    cleaned = re.sub(r"\([^)]*\)", " ", full_name or "")
    parts = [p for p in re.split(r"[\s,]+", cleaned.strip()) if p]
    parts = [p for p in parts if norm_text(p) not in _NAME_SUFFIXES]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def name_key(first: str, last: str, full: str = "") -> str:
    """Identity key for a person's name, ignoring middle names, case and accents."""
    first_tokens = _name_tokens(first)
    last_tokens = _name_tokens(last)
    if not first_tokens or not last_tokens:
        tokens = _name_tokens(full)
        if len(tokens) < 2:
            return ""
        first_tokens, last_tokens = tokens[:1], tokens[-1:]
    return f"{first_tokens[0]}|{''.join(last_tokens)}"


def normalize_linkedin_url(url: str) -> str:
    """Canonical 'linkedin.com/in/<slug>' form, or '' if not a profile URL."""
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return ""
    match = re.match(r"^/(in|pub)/([^/?#]+)", parsed.path, re.IGNORECASE)
    if not match:
        return ""
    return f"linkedin.com/in/{match.group(2).lower()}"


def linkedin_display_url(url: str) -> str:
    normalized = normalize_linkedin_url(url)
    return f"https://www.{normalized}" if normalized else (url or "")


def normalize_domain(value: str) -> str:
    """'https://www.GoldmanSachs.com/careers' -> 'goldmansachs.com'."""
    if not value or not isinstance(value, str):
        return ""
    value = value.strip().lower()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if "://" not in value:
        value = "//" + value
    host = urlparse(value).netloc.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else ""


def is_valid_email(value: str) -> bool:
    return bool(value) and isinstance(value, str) and bool(_EMAIL_RE.match(value.strip()))


def email_local_part(name: str) -> str:
    """Name fragment usable in an email local part: 'O'Brien-Smith' -> 'obriensmith'."""
    return re.sub(r"[^a-z0-9]", "", ascii_fold(name or "").lower())
