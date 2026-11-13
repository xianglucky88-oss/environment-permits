from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Attachment:
    """A link found on a permit page.

    ``url`` is the absolute, canonical resource address resolved against the
    page that contained the link.  ``resource_id`` is the identity of that
    address (currently the canonical URL); ``raw_href`` keeps what the page
    literally said for auditing.  ``title`` is only a human label.
    """

    url: str
    title: str
    resource_id: str = ""
    raw_href: str = ""

    def __post_init__(self) -> None:
        if not self.resource_id:
            object.__setattr__(self, "resource_id", self.url)


@dataclass(frozen=True)
class AttachmentObservation:
    """The result of one attempt to obtain an attachment.

    ``outcome`` is ``"fetched"`` or ``"failed"``.  A failed attempt carries no
    content identity: it is evidence that content could not be obtained, not
    evidence that the content did not change.
    """

    citation_id: str
    resource_id: str
    url: str
    title: str
    outcome: str
    observed_at: str
    raw_href: str = ""
    version_id: str | None = None
    content_id: str | None = None
    media_type: str | None = None
    byte_size: int | None = None
    error: str | None = None
    content_changed: bool | None = None


@dataclass(frozen=True)
class PermitDocument:
    document_id: str
    company_name: str
    permit_number: str
    document_type: str
    issued_at: str
    valid_until: str | None
    pollutants: tuple[str, ...]
    source_url: str
    raw_text: str
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
    collected_at: str = field(default_factory=utc_now)
