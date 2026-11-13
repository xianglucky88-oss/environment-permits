from __future__ import annotations
import re
from html.parser import HTMLParser
from .entities import canonical_company, canonical_permit, document_key
from .models import Attachment, PermitDocument
from .uris import resolve_reference


class _PermitParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.meta: dict[str, str] = {}
        self.links: list[Attachment] = []
        self.text: list[str] = []
        self._href: str | None = None
        self._label: list[str] = []
        self._anchor_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") and values.get("content"):
            self.meta[values["name"]] = values["content"]
        if tag == "a":
            if self._anchor_depth > 0:
                self._anchor_depth += 1
            elif values.get("href") and values["href"].strip():
                self._href = values["href"]
                self._label = []
                self._anchor_depth = 1

    def handle_data(self, data: str) -> None:
        clean = " ".join(data.split())
        if clean:
            self.text.append(clean)
            if self._href is not None:
                self._label.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self._anchor_depth -= 1
            if self._anchor_depth > 0:
                return
            raw_href = self._href.strip()
            self.links.append(Attachment(
                url=resolve_reference(self.base_url, raw_href),
                title=" ".join(self._label) or raw_href,
                raw_href=raw_href,
            ))
            self._href = None
            self._label = []
            self._anchor_depth = 0


class DocumentTextExtractor:
    def extract(self, content: bytes, base_url: str = "") -> str:
        parser = _PermitParser(base_url)
        parser.feed(content.decode("utf-8", errors="replace"))
        return " ".join(parser.text)


def parse_permit(source_url: str, content: bytes) -> PermitDocument:
    parser = _PermitParser(source_url)
    parser.feed(content.decode("utf-8", errors="replace"))
    required = ("company", "permit-number", "issued-at", "document-type")
    missing = [field for field in required if not parser.meta.get(field)]
    if missing:
        raise ValueError(f"missing permit metadata: {', '.join(missing)}")
    text = " ".join(parser.text)
    pollutants = tuple(sorted(set(re.findall(r"污染物[:：]\s*([^。；;]+)", text))))
    company = canonical_company(parser.meta["company"])
    permit = canonical_permit(parser.meta["permit-number"])
    issued = parser.meta["issued-at"].strip()
    return PermitDocument(
        document_id=document_key(company, permit, issued),
        company_name=company,
        permit_number=permit,
        document_type=parser.meta["document-type"].strip(),
        issued_at=issued,
        valid_until=parser.meta.get("valid-until"),
        pollutants=pollutants,
        source_url=source_url,
        raw_text=text,
        attachments=tuple(parser.links),
    )
