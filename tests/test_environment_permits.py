from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from environment_permits.extract import parse_permit
from environment_permits.pipeline import PermitPipeline
from environment_permits.sources import (
    AttachmentFetchError,
    FixtureSource,
    LocalAttachmentSource,
    MappingAttachmentSource,
)
from environment_permits.storage import PermitStore
from environment_permits.uris import canonicalize_reference, resolve_reference


PERMIT_META = (
    '<meta name="company" content="测试（上海）有限公司">'
    '<meta name="permit-number" content="ep 01">'
    '<meta name="document-type" content="permit">'
    '<meta name="issued-at" content="2026-01-01">'
)


def page(links: list[tuple[str, str]] = (), meta: str = PERMIT_META) -> bytes:
    anchors = "".join(f'<a href="{href}">{title}</a>' for href, title in links)
    return f"{meta}<body>污染物：颗粒物。{anchors}</body>".encode()


class DictSource:
    """Serves different page bytes for different references."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages

    def read(self, reference: str) -> bytes:
        return self.pages[reference]


@pytest.fixture
def store(tmp_path: Path):
    permit_store = PermitStore(tmp_path / "permits.sqlite3")
    try:
        yield permit_store
    finally:
        permit_store.close()


def collect(store: PermitStore, pages: dict[str, bytes], reference: str,
            attachment_source=None, include_attachments: bool = True):
    return PermitPipeline(
        DictSource(pages), store, attachment_source
    ).collect_one(reference, include_attachments=include_attachments)


def only_citation(store: PermitStore, document_id: str):
    citations = store.citations(document_id)
    assert len(citations) == 1
    return citations[0]


def test_permit_is_normalized_and_stored(store):
    html = (
        '<meta name="company" content="测试（上海）有限公司">'
        '<meta name="permit-number" content="ep 01">'
        '<meta name="document-type" content="permit">'
        '<meta name="issued-at" content="2026-01-01">污染物：颗粒物。'
    )
    document = PermitPipeline(FixtureSource(html.encode()), store).collect_one("fixture://permit")
    assert document.company_name == "测试 上海 有限公司"
    assert document.permit_number == "EP01"
    assert document.pollutants == ("颗粒物",)
    assert store.connection.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 1


def test_relative_address_is_resolved_against_the_current_page():
    html = page([("permit.pdf", "许可证附件 PDF")])
    first = parse_permit("https://example.org/permits/EP-2026-009/", html)
    second = parse_permit("https://example.org/permits/archive/EP-2026-009/", html)
    assert first.attachments[0].url == "https://example.org/permits/EP-2026-009/permit.pdf"
    assert second.attachments[0].url == "https://example.org/permits/archive/EP-2026-009/permit.pdf"
    assert first.attachments[0].raw_href == "permit.pdf"
    assert resolve_reference("https://example.org/docs/v2/", "../files/x.pdf#p1") == (
        "https://example.org/docs/files/x.pdf"
    )


def test_absolute_and_relative_spellings_share_one_citation(store):
    relative_page = page([("permit.pdf", "许可证附件 PDF")])
    absolute_page = page([
        ("https://example.org/permits/EP-2026-009/permit.pdf", "许可证附件 PDF"),
    ])
    base = "https://example.org/permits/EP-2026-009/"
    source = DictSource({base: relative_page})
    document = PermitPipeline(source, store).collect_one(base)
    first = only_citation(store, document.document_id)

    source.pages[base] = absolute_page
    PermitPipeline(source, store).collect_one(base)
    second = only_citation(store, document.document_id)

    assert first.citation_id == second.citation_id
    assert first.resource_id == second.resource_id
    assert len(store.versions(second.resource_id)) == 0


def test_renaming_the_link_keeps_the_same_citation(store):
    base = "https://example.org/permits/EP-2026-009/"
    source = DictSource({base: page([("permit.pdf", "许可证附件 PDF")])})
    document = PermitPipeline(source, store).collect_one(base)
    first = only_citation(store, document.document_id)

    source.pages[base] = page([("permit.pdf", "许可证扫描件")])
    PermitPipeline(source, store).collect_one(base)
    renamed = only_citation(store, document.document_id)

    assert first.citation_id == renamed.citation_id
    assert renamed.titles == ("许可证扫描件",)
    titles = store.connection.execute(
        "SELECT title, active FROM citation_titles WHERE citation_id = ? ORDER BY rowid",
        (renamed.citation_id,),
    ).fetchall()
    assert titles == [("许可证附件 PDF", 0), ("许可证扫描件", 1)]


def test_title_or_filename_alone_does_not_merge_distinct_addresses(store):
    base = "https://example.org/permits/EP-2026-009/"
    links = [
        ("files/a/report.pdf", "监测报告"),
        ("files/b/report.pdf", "监测报告"),
    ]
    document = collect(store, {base: page(links)}, base)

    assert len(store.citations(document.document_id)) == 2
    assert len({c.resource_id for c in store.citations(document.document_id)}) == 2


def test_same_bytes_at_multiple_addresses_reuse_one_content_copy(store):
    base = "https://example.org/permits/EP-2026-009/"
    links = [
        ("files/a/report.pdf", "监测报告 A"),
        ("files/b/report.pdf", "监测报告 B"),
    ]
    payload = b"%PDF shared payload"
    source = MappingAttachmentSource({
        "https://example.org/permits/EP-2026-009/files/a/report.pdf": payload,
        "https://example.org/permits/EP-2026-009/files/b/report.pdf": payload,
    })
    document = collect(store, {base: page(links)}, base, source)

    citations = store.citations(document.document_id)
    assert len(citations) == 2
    versions = [store.versions(c.resource_id) for c in citations]
    assert all(len(version) == 1 for version in versions)
    content_ids = {version[0]["content_id"] for version in versions}
    assert len(content_ids) == 1
    assert store.connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 1
    assert store.stored_content(content_ids.pop()) == payload


def test_two_permits_citing_the_same_content_keep_separate_citations(store):
    url = "https://example.org/files/shared.pdf"
    payload = b"%PDF shared between permits"
    first_page = page([(url, "附件")])
    second_page = page(
        [(url, "附件")],
        meta=(
            '<meta name="company" content="测试（上海）有限公司">'
            '<meta name="permit-number" content="ep 02">'
            '<meta name="document-type" content="permit">'
            '<meta name="issued-at" content="2026-02-01">'
        ),
    )
    source = MappingAttachmentSource({url: payload})
    first = collect(store, {"https://example.org/one": first_page}, "https://example.org/one", source)
    second = collect(store, {"https://example.org/two": second_page}, "https://example.org/two", source)

    first_citation = only_citation(store, first.document_id)
    second_citation = only_citation(store, second.document_id)
    assert first_citation.citation_id != second_citation.citation_id
    assert first_citation.resource_id == second_citation.resource_id
    assert (store.latest_observation(first_citation.citation_id).content_id
            == store.latest_observation(second_citation.citation_id).content_id)
    assert store.connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 1


def test_replaced_content_at_the_original_address_is_a_new_version(store):
    url = "https://example.org/files/permit.pdf"
    base = "https://example.org/permits/EP-2026-009/"
    source = DictSource({base: page([(url, "附件")])})
    attachments = MappingAttachmentSource({url: b"%PDF v1"})
    document = collect(store, {base: source.pages[base]}, base, attachments)
    citation = only_citation(store, document.document_id)
    first_version = store.versions(url)
    assert len(first_version) == 1

    attachments.entries[url] = b"%PDF v2"
    collect(store, {base: source.pages[base]}, base, attachments)
    versions = store.versions(url)

    assert len(versions) == 2
    assert versions[0]["version_id"] != versions[1]["version_id"]
    assert versions[0]["content_id"] != versions[1]["content_id"]
    latest = store.latest_observation(citation.citation_id)
    assert latest.content_changed is True
    assert latest.version_id == versions[1]["version_id"]
    assert store.stored_content(versions[0]["content_id"]) == b"%PDF v1"
    assert store.stored_content(versions[1]["content_id"]) == b"%PDF v2"


def test_removed_reference_keeps_evidence_and_restores_the_same_identity(store):
    url = "https://example.org/files/permit.pdf"
    base = "https://example.org/permits/EP-2026-009/"
    source = DictSource({base: page([(url, "附件")])})
    attachments = MappingAttachmentSource({url: b"%PDF stable"})
    document = collect(store, {base: source.pages[base]}, base, attachments)
    original = only_citation(store, document.document_id)
    versions_before = store.versions(url)

    source.pages[base] = page([])
    collect(store, {base: source.pages[base]}, base, attachments)
    assert store.citations(document.document_id) == []
    inactive = store.citation(document.document_id, url, active=False)
    assert inactive is not None and inactive.active is False
    assert store.latest_observation(inactive.citation_id).outcome == "fetched"
    assert store.versions(url) == versions_before

    # The citation is restored while attachment collection is interrupted:
    # this must not invent a new version or duplicate the stored content.
    source.pages[base] = page([(url, "附件（恢复）")])
    collect(store, {base: source.pages[base]}, base, attachments, include_attachments=False)
    restored = only_citation(store, document.document_id)
    assert restored.citation_id == original.citation_id
    assert store.versions(url) == versions_before
    assert store.connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 1


def test_failed_fetch_reports_insufficient_evidence_not_unchanged(store):
    url = "https://example.org/files/permit.pdf"
    base = "https://example.org/permits/EP-2026-009/"
    source = DictSource({base: page([(url, "附件")])})
    attachments = MappingAttachmentSource({url: b"%PDF v1"})
    document = collect(store, {base: source.pages[base]}, base, attachments)
    citation = only_citation(store, document.document_id)

    attachments.entries[url] = AttachmentFetchError("source unavailable")
    collect(store, {base: source.pages[base]}, base, attachments)
    failed = store.latest_observation(citation.citation_id)

    assert failed.outcome == "failed"
    assert failed.version_id is None
    assert failed.content_id is None
    assert failed.content_changed is None
    assert "source unavailable" in (failed.error or "")
    assert len(store.versions(url)) == 1
    # The permit and the citation remain locatable after the failure.
    assert store.connection.execute(
        "SELECT COUNT(*) FROM permits WHERE document_id = ?", (document.document_id,)
    ).fetchone()[0] == 1
    assert store.citation(document.document_id, url) is not None


def test_citation_versions_attribute_content_to_its_own_reference(store):
    url = "https://example.org/files/shared.pdf"
    first_page = page([(url, "附件")])
    second_page = page(
        [(url, "附件")],
        meta=(
            '<meta name="company" content="测试（上海）有限公司">'
            '<meta name="permit-number" content="ep 02">'
            '<meta name="document-type" content="permit">'
            '<meta name="issued-at" content="2026-02-01">'
        ),
    )
    attachments = MappingAttachmentSource({url: b"%PDF v1"})
    first = collect(store, {"https://example.org/a": first_page}, "https://example.org/a", attachments)
    first_citation = only_citation(store, first.document_id)

    attachments.entries[url] = b"%PDF v2"
    second = collect(store, {"https://example.org/b": second_page}, "https://example.org/b", attachments)
    second_citation = only_citation(store, second.document_id)

    assert [v["content_id"] for v in store.citation_versions(first_citation.citation_id)] == [
        store.versions(url)[0]["content_id"]
    ]
    assert [v["content_id"] for v in store.citation_versions(second_citation.citation_id)] == [
        store.versions(url)[1]["content_id"]
    ]
    # The second permit's first observation is a baseline, not a replacement
    # of the content its own earlier evidence had.
    assert store.latest_observation(second_citation.citation_id).content_changed is None


def test_interrupted_fetch_between_citations_keeps_earlier_evidence(store):
    first_url = "https://example.org/files/first.pdf"
    second_url = "https://example.org/files/second.pdf"
    base = "https://example.org/permits/EP-2026-009/"
    source = DictSource({base: page([(first_url, "第一份"), (second_url, "第二份")])})

    class InterruptingSource(MappingAttachmentSource):
        def fetch(self, resource_id, url):
            if url == second_url:
                raise KeyboardInterrupt("collector stopped")
            return super().fetch(resource_id, url)

    attachments = InterruptingSource({first_url: b"%PDF first"})
    with pytest.raises(KeyboardInterrupt):
        collect(store, {base: source.pages[base]}, base, attachments)

    # The permit and both citations were saved before the interruption.
    document_id = parse_permit(base, source.pages[base]).document_id
    citations = {c.resource_id: c for c in store.citations(document_id, include_inactive=True)}
    assert set(citations) == {first_url, second_url}
    first = store.latest_observation(citations[first_url].citation_id)
    assert first.outcome == "fetched"
    # The interrupted citation has no evidence at all: no claim either way.
    assert store.observations(citations[second_url].citation_id) == []
    assert store.versions(second_url) == []


def test_never_fetched_attachment_has_no_evidence(store):
    url = "https://example.org/files/permit.pdf"
    base = "https://example.org/permits/EP-2026-009/"
    document = collect(store, {base: page([(url, "附件")])}, base, include_attachments=False)
    citation = only_citation(store, document.document_id)

    assert store.observations(citation.citation_id) == []
    assert store.versions(url) == []

    attachments = MappingAttachmentSource({url: b"%PDF first time"})
    collect(store, {base: page([(url, "附件")])}, base, attachments)
    observation = store.latest_observation(citation.citation_id)
    assert observation.outcome == "fetched"
    assert observation.content_changed is None


def test_explicit_unchanged_content_is_recorded_when_bytes_match(store):
    url = "https://example.org/files/permit.pdf"
    base = "https://example.org/permits/EP-2026-009/"
    source = DictSource({base: page([(url, "附件")])})
    attachments = MappingAttachmentSource({url: b"%PDF v1"})
    document = collect(store, {base: source.pages[base]}, base, attachments)
    citation = only_citation(store, document.document_id)

    collect(store, {base: source.pages[base]}, base, attachments)
    observation = store.latest_observation(citation.citation_id)
    assert observation.outcome == "fetched"
    assert observation.content_changed is False
    assert len(store.versions(url)) == 1


def test_interrupted_content_save_does_not_reuse_previous_bytes(store, monkeypatch):
    url = "https://example.org/files/permit.pdf"
    base = "https://example.org/permits/EP-2026-009/"
    source = DictSource({base: page([(url, "附件")])})
    attachments = MappingAttachmentSource({url: b"%PDF v1"})
    document = collect(store, {base: source.pages[base]}, base, attachments)
    citation = only_citation(store, document.document_id)

    attachments.entries[url] = b"%PDF v2"

    def fail_save(content_id, content, media_type):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store, "_persist_content", fail_save)
    collect(store, {base: source.pages[base]}, base, attachments)

    observation = store.latest_observation(citation.citation_id)
    assert observation.outcome == "failed"
    assert observation.version_id is None
    assert observation.content_id is None
    assert observation.content_changed is None
    assert "content not saved" in (observation.error or "")
    assert len(store.versions(url)) == 1
    assert store.stored_content(store.versions(url)[0]["content_id"]) == b"%PDF v1"


def test_metadata_only_observation_is_not_claimed_as_this_fetch(store):
    from environment_permits.models import AttachmentObservation, utc_now
    from environment_permits.uris import attachment_reference_id, content_version_id

    url = "https://example.org/files/permit.pdf"
    resource_id = canonicalize_reference(url)
    citation_id = attachment_reference_id("doc", resource_id)
    observation = AttachmentObservation(
        citation_id=citation_id,
        resource_id=resource_id,
        url=url,
        title="附件",
        outcome="fetched",
        observed_at=utc_now(),
        version_id=content_version_id(resource_id, "abc"),
        content_id="abc",
    )
    stored = store.record_observation(observation)
    assert stored.outcome == "failed"
    assert stored.version_id is None
    assert store.versions(resource_id) == []


def test_local_attachment_source_serves_relative_and_absolute_forms(tmp_path):
    attachments_dir = tmp_path / "site"
    (attachments_dir / "permits" / "EP-2026-009").mkdir(parents=True)
    payload = b"%PDF local attachment"
    (attachments_dir / "permits" / "EP-2026-009" / "permit.pdf").write_bytes(payload)
    local = LocalAttachmentSource(attachments_dir)

    base = "https://example.org/permits/EP-2026-009/"
    relative = parse_permit(base, page([("permit.pdf", "附件")])).attachments[0]
    absolute_url = resolve_reference(base, "./permit.pdf")
    assert local.fetch(relative.resource_id, relative.url).content == payload
    assert local.fetch(canonicalize_reference(absolute_url), absolute_url).content == payload


def test_local_attachment_source_uses_injected_url_map(tmp_path):
    attachments_dir = tmp_path / "site"
    attachments_dir.mkdir()
    (attachments_dir / "stored.pdf").write_bytes(b"%PDF mapped")
    url = "https://example.org/download?id=42"
    local = LocalAttachmentSource(attachments_dir, url_map={url: "stored.pdf"})
    assert local.fetch(url, url).content == b"%PDF mapped"


def test_legacy_link_only_database_remains_readable(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("""CREATE TABLE permits (
        document_id TEXT PRIMARY KEY, company_name TEXT NOT NULL, permit_number TEXT NOT NULL,
        document_type TEXT NOT NULL, issued_at TEXT NOT NULL, valid_until TEXT,
        pollutants TEXT NOT NULL, source_url TEXT NOT NULL, raw_text TEXT NOT NULL,
        collected_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE attachments (
        document_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
        PRIMARY KEY (document_id, url)
    )""")
    document_id = "测试 上海 有限公司|EP01|2026-01-01"
    connection.execute("INSERT INTO permits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        document_id, "测试 上海 有限公司", "EP01", "permit", "2026-01-01", None,
        "颗粒物", "fixture://permit", "污染物：颗粒物。", "2026-01-01T00:00:00+00:00",
    ))
    connection.execute("INSERT INTO attachments VALUES (?, ?, ?)",
                       (document_id, "https://example.org/files/permit.pdf", "许可证附件 PDF"))
    connection.commit()
    connection.close()

    store = PermitStore(database)
    try:
        citation = only_citation(store, document_id)
        assert citation.url == "https://example.org/files/permit.pdf"
        assert citation.titles == ("许可证附件 PDF",)
        assert store.observations(citation.citation_id) == []
        assert store.connection.execute(
            "SELECT url, title FROM attachments WHERE document_id = ?", (document_id,)
        ).fetchone() == ("https://example.org/files/permit.pdf", "许可证附件 PDF")

        url = citation.url
        collect(store, {
            "https://example.org/permits/EP-2026-009/": page([(url, "新标题")]),
        }, "https://example.org/permits/EP-2026-009/", MappingAttachmentSource({url: b"%PDF"}))
        same = store.citation(document_id, url)
        assert same is not None and same.citation_id == citation.citation_id
        assert same.titles == ("新标题",)
        assert store.latest_observation(same.citation_id).outcome == "fetched"
    finally:
        store.close()
