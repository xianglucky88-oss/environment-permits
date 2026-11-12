from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Attachment:
    url: str
    title: str


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
