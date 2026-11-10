import asyncio
from dataclasses import replace
from io import BytesIO
import inspect
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from docx import Document
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from backend.app import main
from backend.app.ingestion import (
    IngestionError,
    extract_resume_text,
    read_bounded_stream,
    validate_declared_content_length,
)
from backend.app.main import create_app
from backend.app.text_processing import detect_resume_sections, preprocess_job_description


PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _pdf_bytes(objects: list[bytes]) -> bytes:
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    )
    return bytes(output)


def make_pdf_pages(texts: list[str]) -> bytes:
    escaped_texts = [text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for text in texts]
    font_number = 3 + len(texts) * 2
    page_numbers = [3 + index * 2 for index in range(len(texts))]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{number} 0 R' for number in page_numbers)}] /Count {len(texts)} >>".encode(),
    ]
    for index, text in enumerate(escaped_texts):
        page_number = page_numbers[index]
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_number} 0 R >> >> /Contents {page_number + 1} 0 R >>".encode()
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return _pdf_bytes(objects)


def make_docx(text: str = "Synthetic resume text") -> bytes:
    document = Document()
    document.add_paragraph(text)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def make_zip(entries: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def raw_upload(client: TestClient, filename: str, content: bytes, content_type: str):
    return client.post(
        "/resumes/extract",
        content=content,
        headers={"content-type": content_type, "x-upload-filename": filename},
    )


@pytest.fixture
def client(settings, loaded_model_service):
    app = create_app(settings=settings, model_service=loaded_model_service)
    with TestClient(app) as test_client:
        yield test_client


def test_extracts_valid_pdf_with_raw_body(client):
    response = raw_upload(client, "candidate.pdf", make_pdf_pages(["Skills Python SQL"]), PDF_MIME)
    assert response.status_code == 200
    assert response.json()["text"] == "Skills Python SQL"


def test_extracts_valid_docx_with_raw_body_and_sanitizes_filename(client):
    response = raw_upload(client, "../synthetic candidate.docx", make_docx("Summary\nSynthetic profile"), DOCX_MIME)
    assert response.status_code == 200
    assert response.json()["filename"] == "synthetic_candidate.docx"
    assert "Synthetic profile" in response.json()["text"]


def test_rejects_empty_unsupported_mismatched_and_malformed_requests(client):
    empty = raw_upload(client, "empty.pdf", b"", PDF_MIME)
    unsupported = raw_upload(client, "resume.txt", b"text", "text/plain")
    mismatch = raw_upload(client, "resume.pdf", make_pdf_pages(["synthetic"]), "text/plain")
    corrupted = raw_upload(client, "broken.pdf", b"%PDF-not-a-valid-document", PDF_MIME)
    missing_filename = client.post("/resumes/extract", content=b"%PDF-1.4", headers={"content-type": PDF_MIME})
    assert empty.json()["error"]["code"] == "empty_file"
    assert unsupported.status_code == 415
    assert mismatch.json()["error"]["code"] == "invalid_mime_type"
    assert corrupted.status_code == 422
    assert corrupted.json()["error"]["code"] == "corrupted_file"
    assert missing_filename.status_code == 400
    assert missing_filename.json()["error"]["code"] == "missing_filename"


def test_oversized_upload_is_rejected_before_the_next_stream_chunk_is_buffered():
    events: list[str] = []

    async def chunks():
        events.append("first")
        yield b"1234"
        events.append("second")
        yield b"56789"
        events.append("third")
        yield b"ignored"

    with pytest.raises(IngestionError, match="exceeds") as error:
        asyncio.run(read_bounded_stream(chunks(), 8))
    assert error.value.code == "file_too_large"
    assert events == ["first", "second"]


def test_endpoint_rejects_oversized_upload(settings, loaded_model_service):
    app = create_app(settings=replace(settings, max_upload_bytes=16), model_service=loaded_model_service)
    with TestClient(app) as limited_client:
        response = raw_upload(limited_client, "large.pdf", make_pdf_pages(["content exceeding sixteen bytes"]), PDF_MIME)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


def test_declared_content_length_rejects_before_streaming():
    with pytest.raises(IngestionError, match="exceeds") as error:
        validate_declared_content_length("17", 16)
    assert error.value.code == "file_too_large"
    with pytest.raises(IngestionError, match="invalid"):
        validate_declared_content_length("not-a-number", 16)


def test_stream_interruption_is_a_structured_error():
    async def interrupted_chunks():
        yield b"%PDF-"
        raise ClientDisconnect()

    with pytest.raises(IngestionError, match="interrupted") as error:
        asyncio.run(read_bounded_stream(interrupted_chunks(), 1024))
    assert error.value.code == "upload_interrupted"


def test_extraction_endpoint_does_not_use_uploadfile_or_application_temp_files(tmp_path, client, monkeypatch):
    source = inspect.getsource(main)
    assert "UploadFile" not in source
    assert "File(...)" not in source
    monkeypatch.chdir(tmp_path)
    before = set(Path.cwd().iterdir())
    response = raw_upload(client, "candidate.pdf", make_pdf_pages(["Synthetic content"]), PDF_MIME)
    after = set(Path.cwd().iterdir())
    assert response.status_code == 200
    assert after == before


def test_rejects_pdf_limits(settings):
    with pytest.raises(IngestionError, match="page limit"):
        extract_resume_text("pdf", make_pdf_pages(["one", "two"]), max_pdf_pages=1, max_pdf_decoded_stream_bytes=1024, max_extracted_text_chars=100)
    with pytest.raises(IngestionError, match="text limit"):
        extract_resume_text("pdf", make_pdf_pages(["long synthetic text"]), max_pdf_pages=2, max_pdf_decoded_stream_bytes=1024, max_extracted_text_chars=5)


def test_rejects_docx_archive_limits(settings, loaded_model_service):
    entries_limited = replace(settings, max_docx_zip_entries=1)
    total_limited = replace(settings, max_docx_total_uncompressed_bytes=1)
    entry_limited = replace(settings, max_docx_entry_uncompressed_bytes=1)
    document = make_docx("Synthetic content")
    for limited_settings in (entries_limited, total_limited, entry_limited):
        app = create_app(settings=limited_settings, model_service=loaded_model_service)
        with TestClient(app) as limited_client:
            response = raw_upload(limited_client, "candidate.docx", document, DOCX_MIME)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "document_limit_exceeded"


def test_rejects_docx_extracted_text_limit(settings, loaded_model_service):
    app = create_app(settings=replace(settings, max_extracted_text_chars=4), model_service=loaded_model_service)
    with TestClient(app) as limited_client:
        response = raw_upload(limited_client, "candidate.docx", make_docx("long text"), DOCX_MIME)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "document_limit_exceeded"


def test_error_responses_and_application_logs_do_not_echo_sensitive_content(client, caplog):
    sensitive_marker = "SYNTHETIC_PRIVATE_MARKER"
    response = raw_upload(client, "candidate.pdf", f"%PDF-{sensitive_marker}".encode(), PDF_MIME)
    assert response.status_code == 422
    assert sensitive_marker not in response.text
    assert sensitive_marker not in caplog.text


def test_detects_basic_resume_sections():
    text = """Synthetic Candidate
candidate@example.test | +1 555 010 2000
Summary
Builds reliable systems.
Skills
Python\nSQL
Work Experience
Example Systems Engineer
Education
Example University
Projects
Sample API
Certifications
Cloud Certificate
"""
    sections = detect_resume_sections(text)
    assert {"contact_information", "summary", "skills", "work_experience", "education", "projects", "certifications"} == set(sections)


def test_preprocesses_job_description_locally_and_rejects_blank_input(client):
    assert preprocess_job_description(" Senior   Engineer \n\n Build reliable APIs ") == "Senior Engineer\nBuild reliable APIs"
    response = client.post("/job-descriptions/preprocess", json={"text": " \n\t "})
    assert response.status_code == 422
