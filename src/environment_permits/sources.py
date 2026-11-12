from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen


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
