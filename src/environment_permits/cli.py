from __future__ import annotations
import argparse
from pathlib import Path
from .pipeline import PermitPipeline
from .sources import CompositeAttachmentSource, FixtureSource, LocalAttachmentSource, UrlSource
from .storage import PermitStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--url", default="https://example.invalid/permit")
    parser.add_argument("--database", default="environment-permits.sqlite3")
    parser.add_argument(
        "--attachment-dir",
        type=Path,
        help="serve attachment addresses from this local directory instead of the network",
    )
    args = parser.parse_args()
    source = FixtureSource(args.fixture.read_bytes()) if args.fixture else UrlSource()
    attachment_source = None
    if args.attachment_dir:
        attachment_source = CompositeAttachmentSource({
            "http": LocalAttachmentSource(args.attachment_dir),
            "https": LocalAttachmentSource(args.attachment_dir),
            "*": LocalAttachmentSource(args.attachment_dir),
        })
    elif args.fixture:
        local_root = args.fixture.parent / "attachments"
        if not local_root.is_dir():
            local_root = args.fixture.parent
        attachment_source = LocalAttachmentSource(local_root)
    store = PermitStore(args.database)
    try:
        document = PermitPipeline(source, store, attachment_source).collect_one(args.url)
        print({
            "document_id": document.document_id,
            "attachments": len(store.citations(document.document_id)),
        })
    finally:
        store.close()


if __name__ == "__main__":
    main()
