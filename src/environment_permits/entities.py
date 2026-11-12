from __future__ import annotations
import re
import unicodedata


def canonical_company(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().upper()
    normalized = re.sub(r"[（）()、,，。]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def canonical_permit(value: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "", value.upper())


def document_key(company: str, permit: str, issued_at: str) -> str:
    return "|".join((canonical_company(company), canonical_permit(permit), issued_at.strip()))
