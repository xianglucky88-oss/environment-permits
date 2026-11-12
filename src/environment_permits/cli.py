from __future__ import annotations
import argparse
from pathlib import Path
from .pipeline import PermitPipeline
from .sources import FixtureSource, UrlSource
from .storage import PermitStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--url", default="https://example.invalid/permit")
    parser.add_argument("--database", default="environment-permits.sqlite3")
    args = parser.parse_args()
    source = FixtureSource(args.fixture.read_bytes()) if args.fixture else UrlSource()
    store = PermitStore(args.database)
    try:
        document = PermitPipeline(source, store).collect_one(args.url)
        print({"document_id": document.document_id, "attachments": len(document.attachments)})
    finally:
        store.close()


if __name__ == "__main__":
    main()
