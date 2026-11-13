from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from .models import AttachmentObservation, PermitDocument, utc_now
from .uris import (
    attachment_reference_id,
    canonicalize_reference,
    content_digest,
    content_version_id,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS permits (
    document_id TEXT PRIMARY KEY, company_name TEXT NOT NULL, permit_number TEXT NOT NULL,
    document_type TEXT NOT NULL, issued_at TEXT NOT NULL, valid_until TEXT,
    pollutants TEXT NOT NULL, source_url TEXT NOT NULL, raw_text TEXT NOT NULL,
    collected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    document_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
    PRIMARY KEY (document_id, url)
);
CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS citations (
    citation_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    url TEXT NOT NULL,
    raw_href TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (document_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_citations_document ON citations (document_id, active);
CREATE INDEX IF NOT EXISTS idx_citations_resource ON citations (resource_id);
CREATE TABLE IF NOT EXISTS citation_titles (
    citation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (citation_id, title)
);
CREATE TABLE IF NOT EXISTS contents (
    content_id TEXT PRIMARY KEY,
    media_type TEXT,
    byte_size INTEGER,
    first_stored_at TEXT NOT NULL,
    payload_saved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS content_blobs (
    content_id TEXT PRIMARY KEY,
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_versions (
    version_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    media_type TEXT,
    byte_size INTEGER,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    UNIQUE (resource_id, content_id)
);
CREATE INDEX IF NOT EXISTS idx_versions_resource ON resource_versions (resource_id);
CREATE TABLE IF NOT EXISTS observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    citation_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    version_id TEXT,
    error TEXT,
    content_changed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_observations_citation ON observations (citation_id, observation_id);
"""


@dataclass(frozen=True)
class Citation:
    citation_id: str
    document_id: str
    resource_id: str
    url: str
    raw_href: str
    first_seen_at: str
    last_seen_at: str
    active: bool
    titles: tuple[str, ...]


class PermitStore:
    """SQLite persistence for permits, their attachment citations and evidence.

    Three identities are deliberately kept apart:

    * ``resource_id`` — identity of an address (canonical URL).  A renamed link
      to the same address keeps the same resource.
    * ``content_id`` — identity of bytes (SHA-256).  Bytes stored at many
      addresses are saved once and reused.
    * ``citation_id`` — identity of "permit document X cites resource Y".  Two
      permits citing the same content keep separate citations.

    ``version_id`` ties one content variant to one resource address.
    """

    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(_SCHEMA)
        self._migrate_legacy_schema()
        self.connection.commit()

    # ------------------------------------------------------------------ setup

    def _columns(self, table: str) -> set[str]:
        rows = self.connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        return {row[1] for row in rows}

    def _migrate_legacy_schema(self) -> None:
        """Make a database written by the link-only version fully readable.

        Legacy ``attachments`` rows become citations (evidence that a link was
        recorded); the legacy table itself is kept as a mirror so existing
        readers and queries keep working.
        """

        columns = self._columns("attachments")
        additions = {
            "raw_href": "TEXT NOT NULL DEFAULT ''",
            "first_seen_at": "TEXT NOT NULL DEFAULT ''",
            "last_seen_at": "TEXT NOT NULL DEFAULT ''",
            "active": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(
                    f'ALTER TABLE "attachments" ADD COLUMN "{name}" {definition}'
                )

        # Every row is imported idempotently, so reopening a migrated database
        # never duplicates or reactivates evidence.
        legacy = self.connection.execute(
            'SELECT "document_id", "url", "title" FROM "attachments"'
        ).fetchall()
        for document_id, url, title in legacy:
            seen_at_row = self.connection.execute(
                "SELECT collected_at FROM permits WHERE document_id = ?", (document_id,)
            ).fetchone()
            timestamp = seen_at_row[0] if seen_at_row and seen_at_row[0] else utc_now()
            canonical = canonicalize_reference(url)
            self.connection.execute(
                """INSERT OR IGNORE INTO resources (resource_id, canonical_url, first_seen_at)
                   VALUES (?, ?, ?)""",
                (canonical, canonical, timestamp),
            )
            citation_id = attachment_reference_id(document_id, canonical)
            self.connection.execute(
                """INSERT OR IGNORE INTO citations
                   (citation_id, document_id, resource_id, url, raw_href,
                    first_seen_at, last_seen_at, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (citation_id, document_id, canonical, canonical, url, timestamp, timestamp),
            )
            self.connection.execute(
                """INSERT OR IGNORE INTO citation_titles
                   (citation_id, title, first_seen_at, last_seen_at, active)
                   VALUES (?, ?, ?, ?, 1)""",
                (citation_id, title, timestamp, timestamp),
            )

    # -------------------------------------------------------------- recording

    def upsert(
        self,
        document: PermitDocument,
        observations: Iterable[AttachmentObservation] = (),
    ) -> None:
        """Save the permit and its active citations in one transaction.

        Attachments that no longer appear on the page are kept as inactive
        citations with their historical evidence, so a later reappearance
        continues the same identity instead of creating a new file version.
        """

        try:
            for statement, parameters in self._upsert_statements(document):
                self.connection.execute(statement, parameters)
            for observation in observations:
                self._insert_observation(observation)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _upsert_statements(self, document: PermitDocument):
        observed_at = document.collected_at
        yield (
            """INSERT OR REPLACE INTO permits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                document.document_id,
                document.company_name,
                document.permit_number,
                document.document_type,
                document.issued_at,
                document.valid_until,
                ",".join(document.pollutants),
                document.source_url,
                document.raw_text,
                document.collected_at,
            ),
        )

        active: list[tuple[str, str, str, str]] = []
        seen_titles: dict[str, list[str]] = {}
        seen_citations: set[str] = set()
        for attachment in document.attachments:
            url = canonicalize_reference(attachment.url)
            if not url:
                continue
            resource_id = attachment.resource_id or url
            citation_id = attachment_reference_id(document.document_id, resource_id)
            title = attachment.title or attachment.raw_href or url
            seen_citations.add(citation_id)
            seen_titles.setdefault(citation_id, []).append(title)
            active.append((citation_id, url, title, attachment.raw_href))

            yield (
                """INSERT OR IGNORE INTO resources (resource_id, canonical_url, first_seen_at)
                   VALUES (?, ?, ?)""",
                (resource_id, url, observed_at),
            )
            yield (
                """INSERT INTO citations
                   (citation_id, document_id, resource_id, url, raw_href,
                    first_seen_at, last_seen_at, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT (citation_id) DO UPDATE SET
                     url = excluded.url,
                     raw_href = excluded.raw_href,
                     last_seen_at = excluded.last_seen_at,
                     active = 1""",
                (citation_id, document.document_id, resource_id, url,
                 attachment.raw_href, observed_at, observed_at),
            )
            yield (
                """INSERT INTO citation_titles
                   (citation_id, title, first_seen_at, last_seen_at, active)
                   VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT (citation_id, title) DO UPDATE SET
                     last_seen_at = excluded.last_seen_at,
                     active = 1""",
                (citation_id, title, observed_at, observed_at),
            )

        # References that vanished from the page: deactivate, never delete.
        for (citation_id,) in self.connection.execute(
            "SELECT citation_id FROM citations WHERE document_id = ? AND active = 1",
            (document.document_id,),
        ).fetchall():
            if citation_id not in seen_citations:
                yield ("UPDATE citations SET active = 0 WHERE citation_id = ?", (citation_id,))

        for citation_id, titles in seen_titles.items():
            placeholders = ",".join("?" for _ in titles)
            yield (
                f"""UPDATE citation_titles SET active = 0
                    WHERE citation_id = ? AND title NOT IN ({placeholders})""",
                (citation_id, *titles),
            )

        # Legacy mirror: keep the link-only table consistent for old readers.
        yield ('DELETE FROM "attachments" WHERE "document_id" = ?', (document.document_id,))
        for citation_id, url, title, raw_href in active:
            yield (
                """INSERT OR REPLACE INTO "attachments"
                   ("document_id", "url", "title", "raw_href", "first_seen_at",
                    "last_seen_at", "active")
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (document.document_id, url, title, raw_href, observed_at, observed_at),
            )

    def record_observation(
        self,
        observation: AttachmentObservation,
        content: bytes | None = None,
    ) -> AttachmentObservation:
        """Record one fetch attempt, reusing stored bytes by content identity.

        If the bytes cannot be persisted, the attempt is downgraded to a failed
        observation instead of claiming new content: a previously stored
        payload is never presented as this attempt's result.
        """

        if observation.outcome != "fetched":
            self._insert_observation(observation)
            self.connection.commit()
            return observation
        if content is None:
            final = replace(
                observation,
                outcome="failed",
                version_id=None,
                content_id=None,
                byte_size=None,
                content_changed=None,
                error=observation.error or "content bytes unavailable",
            )
            self._insert_observation(final)
            self.connection.commit()
            return final

        digest = observation.content_id or content_digest(content)
        version_id = observation.version_id or content_version_id(
            observation.resource_id, digest
        )
        changed = self._content_changed(observation.citation_id, digest)
        final = replace(
            observation,
            version_id=version_id,
            content_id=digest,
            byte_size=len(content),
            content_changed=changed,
        )
        try:
            self.connection.execute("SAVEPOINT attachment_evidence")
            self._persist_content(digest, content, observation.media_type)
            self._persist_version(final)
            self._insert_observation(final)
            self.connection.execute("RELEASE attachment_evidence")
            self.connection.commit()
            return final
        except Exception as error:
            self.connection.execute("ROLLBACK TO attachment_evidence")
            self.connection.execute("RELEASE attachment_evidence")
            self.connection.rollback()
            failed = replace(
                observation,
                outcome="failed",
                version_id=None,
                content_id=None,
                byte_size=None,
                content_changed=None,
                error=f"content not saved: {error}",
            )
            self._insert_observation(failed)
            self.connection.commit()
            return failed

    def _persist_content(self, content_id: str, content: bytes, media_type: str | None) -> None:
        """Store bytes once per content identity; duplicates are not re-saved."""

        self.connection.execute(
            """INSERT OR IGNORE INTO content_blobs (content_id, content) VALUES (?, ?)""",
            (content_id, content),
        )
        existing = self.connection.execute(
            "SELECT payload_saved FROM contents WHERE content_id = ?", (content_id,)
        ).fetchone()
        if existing is None:
            self.connection.execute(
                """INSERT INTO contents
                   (content_id, media_type, byte_size, first_stored_at, payload_saved)
                   VALUES (?, ?, ?, ?, 1)""",
                (content_id, media_type, len(content), utc_now()),
            )
        elif not existing[0]:
            self.connection.execute(
                """UPDATE contents SET payload_saved = 1,
                   media_type = COALESCE(?, media_type), byte_size = ?
                   WHERE content_id = ?""",
                (media_type, len(content), content_id),
            )
        elif media_type:
            self.connection.execute(
                "UPDATE contents SET media_type = COALESCE(media_type, ?) WHERE content_id = ?",
                (media_type, content_id),
            )

    def _persist_version(self, observation: AttachmentObservation) -> None:
        assert observation.version_id and observation.content_id
        self.connection.execute(
            """INSERT INTO resource_versions
               (version_id, resource_id, content_id, media_type, byte_size,
                first_observed_at, last_observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (version_id) DO UPDATE SET
                 last_observed_at = excluded.last_observed_at,
                 media_type = COALESCE(excluded.media_type, resource_versions.media_type)""",
            (
                observation.version_id,
                observation.resource_id,
                observation.content_id,
                observation.media_type,
                observation.byte_size,
                observation.observed_at,
                observation.observed_at,
            ),
        )

    def _content_changed(self, citation_id: str, content_id: str) -> bool | None:
        """Compare against this citation's own previous successful evidence.

        Another permit's observation of the same address is not evidence this
        citation ever held that content, so it cannot make this citation's
        content look "changed".  Resource-level version history still records
        every variant at the address.
        """

        row = self.connection.execute(
            """SELECT v.content_id FROM observations o
               JOIN resource_versions v ON v.version_id = o.version_id
               WHERE o.citation_id = ? AND o.outcome = 'fetched'
               ORDER BY o.observation_id DESC LIMIT 1""",
            (citation_id,),
        ).fetchone()
        if row is None:
            return None
        return row[0] != content_id

    def _insert_observation(self, observation: AttachmentObservation) -> None:
        self.connection.execute(
            """INSERT INTO observations
               (citation_id, resource_id, observed_at, outcome, version_id, error,
                content_changed)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                observation.citation_id,
                observation.resource_id,
                observation.observed_at,
                observation.outcome,
                observation.version_id,
                observation.error,
                None if observation.content_changed is None else int(observation.content_changed),
            ),
        )

    # ---------------------------------------------------------------- queries

    def citation(
        self, document_id: str, url: str, active: bool | None = True
    ) -> Citation | None:
        """Look up a citation by document and any spelling of its address.

        Renaming the link or switching between relative and absolute spelling
        finds the same citation; a different permit keeps its own citation.
        """

        canonical = canonicalize_reference(url)
        query = """SELECT citation_id, document_id, resource_id, url, raw_href,
                          first_seen_at, last_seen_at, active
                   FROM citations WHERE document_id = ? AND (resource_id = ? OR url = ?)"""
        parameters: list[object] = [document_id, canonical, canonical]
        if active is not None:
            query += " AND active = ?"
            parameters.append(int(active))
        query += " ORDER BY rowid LIMIT 1"
        row = self.connection.execute(query, parameters).fetchone()
        return None if row is None else self._citation_from_row(row)

    def citations(
        self, document_id: str, include_inactive: bool = False
    ) -> list[Citation]:
        query = """SELECT citation_id, document_id, resource_id, url, raw_href,
                          first_seen_at, last_seen_at, active
                   FROM citations WHERE document_id = ?"""
        parameters: list[object] = [document_id]
        if not include_inactive:
            query += " AND active = 1"
        query += " ORDER BY rowid"
        return [self._citation_from_row(row) for row in self.connection.execute(query, parameters)]

    def _citation_from_row(self, row: Sequence) -> Citation:
        titles = self.connection.execute(
            """SELECT title FROM citation_titles
               WHERE citation_id = ? AND active = 1 ORDER BY rowid""",
            (row[0],),
        ).fetchall()
        return Citation(
            citation_id=row[0],
            document_id=row[1],
            resource_id=row[2],
            url=row[3],
            raw_href=row[4],
            first_seen_at=row[5],
            last_seen_at=row[6],
            active=bool(row[7]),
            titles=tuple(title for (title,) in titles),
        )

    def observations(self, citation_id: str) -> list[AttachmentObservation]:
        url = self._citation_url(citation_id)
        rows = self.connection.execute(
            """SELECT citation_id, resource_id, observed_at, outcome, version_id,
                      content_changed, error
               FROM observations WHERE citation_id = ? ORDER BY observation_id""",
            (citation_id,),
        ).fetchall()
        return [
            AttachmentObservation(
                citation_id=row[0],
                resource_id=row[1],
                url=url,
                title="",
                outcome=row[3],
                observed_at=row[2],
                version_id=row[4],
                error=row[6],
                content_changed=None if row[5] is None else bool(row[5]),
            )
            for row in rows
        ]

    def latest_observation(self, citation_id: str) -> AttachmentObservation | None:
        row = self.connection.execute(
            """SELECT citation_id, resource_id, observed_at, outcome, version_id,
                      content_changed, error
               FROM observations WHERE citation_id = ?
               ORDER BY observation_id DESC LIMIT 1""",
            (citation_id,),
        ).fetchone()
        if row is None:
            return None
        return AttachmentObservation(
            citation_id=row[0],
            resource_id=row[1],
            url=self._citation_url(row[0]),
            title="",
            outcome=row[3],
            observed_at=row[2],
            version_id=row[4],
            error=row[6],
            content_changed=None if row[5] is None else bool(row[5]),
        )

    def _citation_url(self, citation_id: str) -> str:
        row = self.connection.execute(
            "SELECT url FROM citations WHERE citation_id = ?", (citation_id,)
        ).fetchone()
        return row[0] if row else ""

    def versions(self, resource_id: str) -> list[dict[str, object]]:
        cursor = self.connection.execute(
            """SELECT version_id, resource_id, content_id, media_type, byte_size,
                      first_observed_at, last_observed_at
               FROM resource_versions WHERE resource_id = ?
               ORDER BY first_observed_at, rowid""",
            (resource_id,),
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def citation_versions(self, citation_id: str) -> list[dict[str, object]]:
        """Versions this citation has actually observed, oldest first.

        A version observed by another permit citing the same address is not
        attributed to this citation.
        """

        cursor = self.connection.execute(
            """SELECT v.version_id, v.resource_id, v.content_id, v.media_type,
                      v.byte_size, MIN(o.observed_at) AS observed_at
               FROM observations o
               JOIN resource_versions v ON v.version_id = o.version_id
               WHERE o.citation_id = ? AND o.outcome = 'fetched'
               GROUP BY v.version_id, v.resource_id, v.content_id, v.media_type, v.byte_size
               ORDER BY observed_at, v.version_id""",
            (citation_id,),
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def stored_content(self, content_id: str) -> bytes | None:
        """Return stored bytes, or ``None`` when this copy is unavailable."""

        row = self.connection.execute(
            """SELECT b.content FROM content_blobs b
               JOIN contents c ON c.content_id = b.content_id
               WHERE b.content_id = ? AND c.payload_saved = 1""",
            (content_id,),
        ).fetchone()
        return None if row is None else bytes(row[0])

    def close(self) -> None:
        self.connection.close()
