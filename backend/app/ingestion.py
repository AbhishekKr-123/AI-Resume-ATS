"""In-memory validation and text extraction for supported resume documents."""

from __future__ import annotations

from collections.abc import AsyncIterable
from io import BytesIO
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile

from docx import Document
from pypdf import PdfReader, apply_configuration
from starlette.requests import ClientDisconnect


class IngestionError(ValueError):
    """A safe, client-facing resume ingestion error."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


DOCUMENT_TYPES = {
    ".pdf": {
        "name": "pdf",
        "mime_types": {"application/pdf", "application/x-pdf"},
    },
    ".docx": {
        "name": "docx",
        "mime_types": {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
    },
}

_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(filename: str | None) -> str:
    """Strip path components and unsafe characters from a client filename."""
    base_name = Path(filename or "upload").name
    sanitized = _FILENAME_RE.sub("_", base_name).strip("._")
    return (sanitized or "upload")[:128]


async def read_bounded_stream(
    chunks: AsyncIterable[bytes], max_upload_bytes: int
) -> bytes:
    """Read an ASGI request stream without storing more than the configured limit."""
    buffer = bytearray()
    try:
        async for chunk in chunks:
            if len(buffer) + len(chunk) > max_upload_bytes:
                raise IngestionError(
                    "file_too_large",
                    "The uploaded file exceeds the configured size limit.",
                    413,
                )
            buffer.extend(chunk)
    except IngestionError:
        raise
    except ClientDisconnect as exc:
        raise IngestionError("upload_interrupted", "The upload was interrupted.", 400) from exc
    except Exception as exc:
        raise IngestionError("malformed_request", "The upload could not be read.", 400) from exc
    return bytes(buffer)


def _validate_pdf_signature(content: bytes) -> None:
    if not content.startswith(b"%PDF-"):
        raise IngestionError("invalid_signature", "The uploaded PDF has an invalid file signature.")


def _validate_docx_signature(
    content: bytes,
    max_entries: int,
    max_total_uncompressed_bytes: int,
    max_entry_uncompressed_bytes: int,
) -> None:
    if not content.startswith(b"PK\x03\x04"):
        raise IngestionError("invalid_signature", "The uploaded DOCX has an invalid file signature.")
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
    except BadZipFile as exc:
        raise IngestionError("corrupted_file", "The uploaded DOCX is corrupted.") from exc
    if len(entries) > max_entries:
        raise IngestionError("document_limit_exceeded", "The DOCX has too many archive entries.", 413)

    total_uncompressed_bytes = 0
    for entry in entries:
        if entry.file_size > max_entry_uncompressed_bytes:
            raise IngestionError(
                "document_limit_exceeded", "A DOCX archive entry exceeds the configured size limit.", 413
            )
        total_uncompressed_bytes += entry.file_size
        if total_uncompressed_bytes > max_total_uncompressed_bytes:
            raise IngestionError(
                "document_limit_exceeded", "The DOCX exceeds the configured uncompressed size limit.", 413
            )

    names = {entry.filename for entry in entries}
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise IngestionError("invalid_signature", "The uploaded DOCX is not a valid Word document.")


def validate_document(
    filename: str | None,
    content_type: str | None,
    content: bytes,
    max_upload_bytes: int,
    max_docx_zip_entries: int,
    max_docx_total_uncompressed_bytes: int,
    max_docx_entry_uncompressed_bytes: int,
) -> tuple[str, str]:
    """Validate extension, MIME type, byte limit, and file signature."""
    sanitized_filename = sanitize_filename(filename)
    extension = Path(sanitized_filename).suffix.lower()
    document_type = DOCUMENT_TYPES.get(extension)

    if document_type is None:
        raise IngestionError("unsupported_file_type", "Only PDF and DOCX resume files are supported.", 415)
    if not content:
        raise IngestionError("empty_file", "The uploaded file is empty.")
    if len(content) > max_upload_bytes:
        raise IngestionError("file_too_large", "The uploaded file exceeds the configured size limit.", 413)
    if content_type not in document_type["mime_types"]:
        raise IngestionError("invalid_mime_type", "The uploaded file MIME type does not match its extension.", 415)

    if extension == ".pdf":
        _validate_pdf_signature(content)
    else:
        _validate_docx_signature(
            content,
            max_docx_zip_entries,
            max_docx_total_uncompressed_bytes,
            max_docx_entry_uncompressed_bytes,
        )
    return sanitized_filename, document_type["name"]


def validate_declared_content_length(
    declared_content_length: str | None, max_upload_bytes: int
) -> None:
    """Reject an unambiguously oversized request before consuming its body.

    The streaming limit remains authoritative because Content-Length is optional
    and client-controlled.
    """
    if declared_content_length is None:
        return
    try:
        content_length = int(declared_content_length)
    except ValueError as exc:
        raise IngestionError("malformed_request", "The Content-Length header is invalid.", 400) from exc
    if content_length < 0:
        raise IngestionError("malformed_request", "The Content-Length header is invalid.", 400)
    if content_length > max_upload_bytes:
        raise IngestionError(
            "file_too_large", "The uploaded file exceeds the configured size limit.", 413
        )


def _require_text(text: str, max_extracted_text_chars: int) -> str:
    normalized = text.strip()
    if not normalized:
        raise IngestionError(
            "unreadable_text", "The document does not contain readable extractable text."
        )
    if len(normalized) > max_extracted_text_chars:
        raise IngestionError(
            "document_limit_exceeded",
            "The document exceeds the configured extracted text limit.",
            413,
        )
    return normalized


def extract_resume_text(
    document_type: str,
    content: bytes,
    max_pdf_pages: int,
    max_pdf_decoded_stream_bytes: int,
    max_extracted_text_chars: int,
) -> str:
    """Extract text exclusively from the in-memory document bytes."""
    try:
        if document_type == "pdf":
            with apply_configuration(
                maximum_declared_stream_length=max_pdf_decoded_stream_bytes,
                array_based_stream_maximum_output_length=max_pdf_decoded_stream_bytes,
                jbig2_maximum_output_length=max_pdf_decoded_stream_bytes,
                lzw_maximum_output_length=max_pdf_decoded_stream_bytes,
                run_length_maximum_output_length=max_pdf_decoded_stream_bytes,
                zlib_maximum_output_length=max_pdf_decoded_stream_bytes,
                brotli_maximum_output_length=max_pdf_decoded_stream_bytes,
                image_maximum_buffer_size=max_pdf_decoded_stream_bytes,
                xmp_maximum_input_length=max_pdf_decoded_stream_bytes,
                stream_decoding_work_maximum_length=max_pdf_decoded_stream_bytes * 3,
                stream_parsing_work_maximum_count=max_extracted_text_chars * 4,
                page_tree_maximum_entries=max_pdf_pages * 10,
                page_tree_maximum_page_entries=max_pdf_pages * 10,
            ):
                reader = PdfReader(
                    BytesIO(content),
                    strict=True,
                    root_object_recovery_limit=max_pdf_pages * 10,
                    xref_stream_maximum_entries=max_pdf_pages * 1_000,
                )
                if reader.is_encrypted:
                    raise IngestionError(
                        "unreadable_text", "Password-protected PDFs are not supported."
                    )
                if len(reader.pages) > max_pdf_pages:
                    raise IngestionError(
                        "document_limit_exceeded",
                        "The PDF exceeds the configured page limit.",
                        413,
                    )
                text_parts: list[str] = []
                text_length = 0
                for page in reader.pages:
                    page_text = page.extract_text() or ""
                    text_length += len(page_text)
                    if text_length > max_extracted_text_chars:
                        raise IngestionError(
                            "document_limit_exceeded",
                            "The document exceeds the configured extracted text limit.",
                            413,
                        )
                    text_parts.append(page_text)
                text = "\n".join(text_parts)
        elif document_type == "docx":
            document = Document(BytesIO(content))
            text_parts = []
            text_length = 0
            for paragraph in document.paragraphs:
                text_length += len(paragraph.text)
                if text_length > max_extracted_text_chars:
                    raise IngestionError(
                        "document_limit_exceeded",
                        "The document exceeds the configured extracted text limit.",
                        413,
                    )
                text_parts.append(paragraph.text)
            text = "\n".join(text_parts)
        else:
            raise IngestionError("unsupported_file_type", "Only PDF and DOCX resume files are supported.", 415)
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError("corrupted_file", "The uploaded document could not be read.") from exc
    return _require_text(text, max_extracted_text_chars)
