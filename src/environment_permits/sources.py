from __future__ import annotations
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from .uris import canonicalize_reference


class DocumentSource:
    def read(self, reference: str) -> bytes:
        raise NotImplementedError


@dataclass
class FixtureSource(DocumentSource):
    content: bytes

    def read(self, reference: str) -> bytes:
        return self.content


@dataclass
class UrlSource(DocumentSource):
    timeout_seconds: float = 15.0

    def read(self, reference: str) -> bytes:
        request = Request(reference, headers={"User-Agent": "environment-permits/0.1"})
        with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
            return response.read()


@dataclass(frozen=True)
class AttachmentPayload:
    content: bytes
    media_type: str | None = None


class AttachmentFetchError(Exception):
    """Raised when attachment content cannot be obtained."""


class AttachmentSource:
    """Injectable provider of attachment bytes.

    Implementations must raise instead of returning a stand-in when the
    resource cannot be obtained: a failed fetch is evidence that the content is
    unknown, never evidence that it is unchanged.
    """

    def fetch(self, resource_id: str, url: str) -> AttachmentPayload:
        raise NotImplementedError


@dataclass
class MappingAttachmentSource(AttachmentSource):
    """Maps addresses to bytes; a string exception value simulates failure."""

    entries: dict[str, bytes | AttachmentPayload | Exception]

    def fetch(self, resource_id: str, url: str) -> AttachmentPayload:
        for key in (url, resource_id):
            if key in self.entries:
                value = self.entries[key]
                break
        else:
            raise AttachmentFetchError(f"no local attachment for {url}")
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            return AttachmentPayload(value)
        return value


@dataclass
class LocalAttachmentSource(AttachmentSource):
    """Serves attachments from a local directory without touching the network.

    By default the address path is read as a path relative to ``directory``
    (as a static website would serve it), so both a relative link resolved
    against the page and the equivalent absolute address read the same file.
    ``url_map`` may override individual addresses with a relative file name
    inside the directory.  Addresses escaping the directory are rejected.
    """

    directory: Path
    url_map: dict[str, str] = dataclasses.field(default_factory=dict)
    media_types: dict[str, str] = dataclasses.field(default_factory=dict)

    def _safe_path(self, relative: str, url: str) -> Path:
        root = Path(self.directory).resolve()
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise AttachmentFetchError(f"attachment address escapes local root: {url}")
        return target

    def _local_path(self, url: str) -> Path:
        mapped = self.url_map.get(url) or self.url_map.get(canonicalize_reference(url))
        if mapped is not None:
            return self._safe_path(mapped, url)
        path = unquote(urlsplit(url).path)
        relative = path.lstrip("/")
        if not relative:
            raise AttachmentFetchError(f"attachment address has no path: {url}")
        return self._safe_path(relative, url)

    def fetch(self, resource_id: str, url: str) -> AttachmentPayload:
        target = self._local_path(url)
        try:
            content = target.read_bytes()
        except OSError as error:
            raise AttachmentFetchError(f"cannot read {url}: {error}") from error
        media_type = self.media_types.get(url) or self.media_types.get(resource_id)
        return AttachmentPayload(content, media_type)


@dataclass
class CompositeAttachmentSource(AttachmentSource):
    """Routes addresses to injected sources by URL scheme."""

    routes: dict[str, AttachmentSource]

    def fetch(self, resource_id: str, url: str) -> AttachmentPayload:
        scheme = urlsplit(url).scheme.lower()
        source = self.routes.get(scheme)
        if source is None:
            source = self.routes.get("*")
        if source is None:
            raise AttachmentFetchError(f"no attachment source for {scheme or 'relative'} address")
        return source.fetch(resource_id, url)
