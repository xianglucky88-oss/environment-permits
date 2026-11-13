from __future__ import annotations

import hashlib
import posixpath
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

# Schemes for which :mod:`urllib.parse` performs standard relative resolution.
_STANDARD_SCHEMES = frozenset({"http", "https", "ftp", "file", "ws", "wss"})
_DEFAULT_PORTS = {"http": "80", "https": "443", "ftp": "21", "ws": "80", "wss": "443"}


def _canonical_netloc(netloc: str, scheme: str) -> str:
    if not netloc:
        return netloc
    userinfo, separator, hostport = netloc.rpartition("@")
    prefix = f"{userinfo}@" if separator else ""
    if hostport.startswith("["):
        host, _, remainder = hostport.partition("]")
        host = f"{host}]"
        port = remainder[1:] if remainder.startswith(":") else ""
    else:
        host, separator, port = hostport.partition(":")
        if not separator:
            port = ""
    host = host.lower()
    if _DEFAULT_PORTS.get(scheme) == port:
        port = ""
    return f"{prefix}{host}{':' + port if port else ''}"


def _normalize_path(path: str) -> str:
    if not path:
        return path
    trailing_slash = path.endswith("/")
    normalized = posixpath.normpath(path)
    if normalized == ".":
        normalized = ""
    if path.startswith("/") and normalized and not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if trailing_slash and normalized and not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return normalized


def canonicalize_reference(url: str) -> str:
    """Return the stable identity of a resource address.

    Fragments identify a position inside a resource rather than another
    resource, so they are removed.  Scheme and host are case-insensitive,
    default ports and dot segments are normalized.  Query strings are kept:
    they may distinguish genuinely different resources.
    """

    cleaned = url.strip()
    if not cleaned:
        return cleaned
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    netloc = _canonical_netloc(parts.netloc, scheme)
    path = _normalize_path(parts.path)
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def resolve_reference(base_url: str, href: str) -> str:
    """Resolve ``href`` against the page that contained it.

    ``base_url`` is the reference of the page currently being collected, not a
    historical or configured address.  Custom schemes such as ``fixture://``
    are supported by temporarily substituting a standard scheme during the
    join and restoring the original one afterwards.
    """

    if not href or not href.strip():
        return ""
    cleaned_href = href.strip()
    href_parts = urlsplit(cleaned_href)
    base_parts = urlsplit(base_url.strip())
    base_scheme = base_parts.scheme.lower()

    if href_parts.scheme:
        resolved = cleaned_href
    elif cleaned_href.startswith("//"):
        if not base_scheme:
            return canonicalize_reference(cleaned_href)
        resolved = f"{base_scheme}:{cleaned_href}"
    elif not base_scheme:
        # Without a page address there is nothing to resolve against.
        return canonicalize_reference(cleaned_href)
    elif base_scheme in _STANDARD_SCHEMES:
        resolved = urljoin(base_url.strip(), cleaned_href)
    else:
        placeholder = "http"
        rewritten_base = urlunsplit(
            (placeholder, base_parts.netloc, base_parts.path, base_parts.query, "")
        )
        joined = urljoin(rewritten_base, cleaned_href)
        joined_parts = urlsplit(joined)
        resolved = urlunsplit(
            (base_scheme, joined_parts.netloc, joined_parts.path, joined_parts.query, "")
        )
    return canonicalize_reference(resolved)


def content_digest(content: bytes) -> str:
    """Content identity: the same bytes always have the same digest."""

    return hashlib.sha256(content).hexdigest()


def attachment_reference_id(document_id: str, resource_id: str) -> str:
    """Stable identity of "document X cites resource Y".

    Title, file name and observing order are deliberately absent: a renamed
    link to the same address remains the same reference.
    """

    basis = f"reference\x00{document_id}\x00{resource_id}".encode("utf-8")
    return hashlib.sha256(basis).hexdigest()


def content_version_id(resource_id: str, digest: str) -> str:
    """Identity of a content variant observed at one resource address."""

    basis = f"version\x00{resource_id}\x00{digest}".encode("utf-8")
    return hashlib.sha256(basis).hexdigest()


def guess_media_type(url: str) -> str:
    path = unquote(urlsplit(url).path).lower()
    if path.endswith(".pdf"):
        return "application/pdf"
    if path.endswith(".html") or path.endswith(".htm"):
        return "text/html"
    if path.endswith(".json"):
        return "application/json"
    return "application/octet-stream"
