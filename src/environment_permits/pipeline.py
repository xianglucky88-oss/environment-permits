from __future__ import annotations

from .extract import parse_permit
from .models import AttachmentObservation, PermitDocument, utc_now
from .sources import AttachmentFetchError, AttachmentPayload, AttachmentSource, DocumentSource
from .storage import PermitStore
from .uris import attachment_reference_id, content_digest, content_version_id


def _error_text(error: BaseException) -> str:
    if isinstance(error, AttachmentFetchError):
        return str(error) or error.__class__.__name__
    return f"{error.__class__.__name__}: {error}"


class PermitPipeline:
    """Collect a permit page, then obtain each attachment it cites.

    Evidence is recorded per citation (document + address): a title change or
    a relative/absolute spelling of the same address never creates a second
    attachment, while two permits citing the same address keep separate
    citations.  The permit and its citations are committed before any
    attachment is fetched, so a document whose attachment fails is still
    locatable by citation; the failure is then stored on that citation alone.
    """

    def __init__(
        self,
        source: DocumentSource,
        store: PermitStore,
        attachment_source: AttachmentSource | None = None,
    ) -> None:
        self.source = source
        self.store = store
        self.attachment_source = attachment_source

    def collect_one(self, reference: str, include_attachments: bool = True) -> PermitDocument:
        document = parse_permit(reference, self.source.read(reference))

        # Commit the permit page and every citation before fetching anything.
        # A later attachment failure therefore cannot hide the document, and a
        # crash before an attempt leaves "no evidence yet" rather than a claim
        # that the content is unchanged.
        self.store.upsert(document)
        if not include_attachments or self.attachment_source is None:
            return document

        for attachment in document.attachments:
            resource_id = attachment.resource_id or attachment.url
            citation_id = attachment_reference_id(document.document_id, resource_id)
            payload: AttachmentPayload | None = None
            failure: str | None = None
            try:
                payload = self.attachment_source.fetch(resource_id, attachment.url)
            except Exception as error:  # fetch failures are evidence, not crashes
                failure = _error_text(error)

            if payload is None:
                self.store.record_observation(AttachmentObservation(
                    citation_id=citation_id,
                    resource_id=resource_id,
                    url=attachment.url,
                    title=attachment.title,
                    raw_href=attachment.raw_href,
                    outcome="failed",
                    observed_at=utc_now(),
                    error=failure or "attachment content unavailable",
                ))
                continue

            digest = content_digest(payload.content)
            observation = AttachmentObservation(
                citation_id=citation_id,
                resource_id=resource_id,
                url=attachment.url,
                title=attachment.title,
                raw_href=attachment.raw_href,
                outcome="fetched",
                observed_at=utc_now(),
                version_id=content_version_id(resource_id, digest),
                content_id=digest,
                media_type=payload.media_type,
                byte_size=len(payload.content),
            )
            # Bytes are reused by digest when this content is already stored;
            # if they cannot be saved, this attempt becomes a failed
            # observation instead of reusing a previous successful result.
            self.store.record_observation(observation, payload.content)
        return document
