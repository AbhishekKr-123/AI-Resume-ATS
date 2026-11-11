"""FastAPI entry point for health checks and in-memory resume ingestion."""

from __future__ import annotations

import base64
import binascii
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import ConfigurationError, Settings, load_settings
from .auth import optional_authenticated_user, require_authenticated_user
from .database_service import AuthenticatedUser, DatabaseError, SupabaseService
from .feedback_service import FeedbackError, GroqFeedbackService
from .ingestion import (
    IngestionError,
    extract_resume_text,
    read_bounded_stream,
    validate_declared_content_length,
    validate_document,
)
from .model_service import LocalModelService, ModelLoadError
from .rate_limit import RequestRateLimiter
from .scoring.ats_scorer import ATSScorer
from .text_processing import detect_resume_sections, preprocess_job_description

logger = logging.getLogger(__name__)


class ResumeExtractionResponse(BaseModel):
    filename: str
    document_type: str
    text: str
    character_count: int
    sections: dict[str, str]


class JobDescriptionRequest(BaseModel):
    text: str


class JobDescriptionResponse(BaseModel):
    text: str
    character_count: int


class ResumeAnalysisResponse(BaseModel):
    filename: str
    document_type: str
    analysis: dict
    analysis_id: str | None = None


class ResumeFeedbackResponse(ResumeAnalysisResponse):
    feedback: dict


def _decode_utf8_header(value: str, label: str) -> str:
    """Strictly decode an ASCII URL-safe Base64 header as UTF-8."""
    try:
        decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        return decoded.decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, ValueError) as exc:
        raise IngestionError(
            "invalid_header_encoding",
            f"The {label} header is not valid URL-safe Base64 UTF-8.",
            400,
        ) from exc


def _text_header(request: Request, encoded_name: str, legacy_name: str, label: str) -> str | None:
    """Prefer Unicode-safe transport while retaining ASCII legacy compatibility."""
    encoded = request.headers.get(encoded_name)
    if encoded is not None:
        return _decode_utf8_header(encoded, label)
    return request.headers.get(legacy_name)


def create_app(
    settings: Settings | None = None,
    model_service: LocalModelService | None = None,
    feedback_service: GroqFeedbackService | None = None,
    supabase_service: SupabaseService | None = None,
) -> FastAPI:
    """Create the API, loading the local model once within its lifespan."""
    resolved_settings = settings or load_settings()
    service = model_service or LocalModelService(resolved_settings.model_path)
    configured_feedback_service = feedback_service
    configured_supabase_service = supabase_service
    analysis_limiter = RequestRateLimiter(resolved_settings.analysis_rate_limit_requests, resolved_settings.analysis_rate_limit_window_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            service.load()
        except (ConfigurationError, ModelLoadError) as exc:
            logger.error("Local fine-tuned model startup verification failed: %s", exc)
            raise RuntimeError("Local fine-tuned model startup verification failed.") from exc

        app.state.model_service = service
        if configured_supabase_service is not None:
            app.state.supabase_service = configured_supabase_service
        elif resolved_settings.supabase_url and resolved_settings.supabase_publishable_key:
            app.state.supabase_service = SupabaseService(resolved_settings.supabase_url, resolved_settings.supabase_publishable_key, resolved_settings.supabase_timeout_seconds)
        else:
            app.state.supabase_service = None
        if configured_feedback_service is not None:
            app.state.feedback_service = configured_feedback_service
        elif resolved_settings.groq_api_key and resolved_settings.groq_model:
            app.state.feedback_service = GroqFeedbackService(
                resolved_settings.groq_api_key,
                resolved_settings.groq_model,
                resolved_settings.groq_timeout_seconds,
            )
        else:
            app.state.feedback_service = None
        logger.info("Local fine-tuned model startup verification completed.")
        yield
        database: SupabaseService | None = getattr(app.state, "supabase_service", None)
        if database is not None:
            database.close()

    app = FastAPI(title="AI Resume ATS API", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def limit_expensive_analysis_requests(request: Request, call_next):
        if request.url.path in {"/resumes/analyze", "/resumes/feedback"}:
            client = request.client.host if request.client else "unknown"
            if not analysis_limiter.allow(client):
                return JSONResponse(status_code=429, content={"error": {"code": "rate_limited", "message": "Too many analysis requests. Please try again later."}})
        return await call_next(request)

    @app.exception_handler(IngestionError)
    async def ingestion_error_handler(_: Request, exc: IngestionError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(FeedbackError)
    async def feedback_error_handler(_: Request, exc: FeedbackError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(DatabaseError)
    async def database_error_handler(_: Request, __: DatabaseError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": {"code": "storage_unavailable", "message": "Analysis storage is currently unavailable."}})

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def readiness(request: Request):
        loaded_service: LocalModelService | None = getattr(
            request.app.state, "model_service", None
        )
        if loaded_service is None or not loaded_service.is_ready:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready"},
            )
        return {
            "status": "ready",
            "model": "local-finetuned",
            "embedding_dimension": loaded_service.embedding_dimension,
        }

    # @app.post("/resumes/extract", response_model=ResumeExtractionResponse)
    @app.post(
    "/resumes/extract",
    response_model=ResumeExtractionResponse,
    openapi_extra={
        "parameters": [
            {
                "name": "X-Upload-Filename-Base64",
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
                "description": "Original filename, e.g. resume.pdf",
            }
        ],
        "requestBody": {
            "required": True,
            "content": {
                "application/pdf": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                    }
                },
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                    }
                },
            },
        },
    },
)
    async def extract_resume(request: Request) -> ResumeExtractionResponse:
        """Stream, validate, and extract a raw document without multipart file spooling."""
        filename = _text_header(request, "x-upload-filename-base64", "x-upload-filename", "upload filename")
        if not filename:
            raise IngestionError(
                "missing_filename", "An X-Upload-Filename-Base64 header is required.", 400
            )
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        validate_declared_content_length(
            request.headers.get("content-length"), resolved_settings.max_upload_bytes
        )
        content = await read_bounded_stream(request.stream(), resolved_settings.max_upload_bytes)
        sanitized_filename, document_type = validate_document(
            filename=filename,
            content_type=content_type,
            content=content,
            max_upload_bytes=resolved_settings.max_upload_bytes,
            max_docx_zip_entries=resolved_settings.max_docx_zip_entries,
            max_docx_total_uncompressed_bytes=resolved_settings.max_docx_total_uncompressed_bytes,
            max_docx_entry_uncompressed_bytes=resolved_settings.max_docx_entry_uncompressed_bytes,
        )
        text = extract_resume_text(
            document_type,
            content,
            max_pdf_pages=resolved_settings.max_pdf_pages,
            max_pdf_decoded_stream_bytes=resolved_settings.max_pdf_decoded_stream_bytes,
            max_extracted_text_chars=resolved_settings.max_extracted_text_chars,
        )
        sections = detect_resume_sections(text)
        return ResumeExtractionResponse(
            filename=sanitized_filename,
            document_type=document_type,
            text=text,
            character_count=len(text),
            sections=sections,
        )

    @app.post("/resumes/analyze", response_model=ResumeAnalysisResponse)
    async def analyze_resume(request: Request) -> ResumeAnalysisResponse:
        """Analyze one securely streamed resume using only the lifespan-loaded model."""
        authenticated = await optional_authenticated_user(request)
        filename = _text_header(request, "x-upload-filename-base64", "x-upload-filename", "upload filename")
        if not filename:
            raise IngestionError("missing_filename", "An X-Upload-Filename-Base64 header is required.", 400)
        raw_job_description = _text_header(request, "x-job-description-base64", "x-job-description", "job description")
        if raw_job_description is None:
            raise IngestionError("missing_job_description", "An X-Job-Description-Base64 header is required.", 400)
        if len(raw_job_description) > resolved_settings.max_job_description_chars:
            raise IngestionError("job_description_too_large", "The job description exceeds the configured size limit.", 413)
        try:
            job_description = preprocess_job_description(raw_job_description)
        except ValueError as exc:
            raise IngestionError("unreadable_text", str(exc)) from exc
        loaded_service: LocalModelService | None = getattr(request.app.state, "model_service", None)
        if loaded_service is None or not loaded_service.is_ready:
            raise IngestionError("model_not_ready", "The local scoring model is not ready.", 503)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        validate_declared_content_length(request.headers.get("content-length"), resolved_settings.max_upload_bytes)
        content = await read_bounded_stream(request.stream(), resolved_settings.max_upload_bytes)
        sanitized_filename, document_type = validate_document(
            filename, content_type, content, resolved_settings.max_upload_bytes,
            resolved_settings.max_docx_zip_entries, resolved_settings.max_docx_total_uncompressed_bytes,
            resolved_settings.max_docx_entry_uncompressed_bytes,
        )
        text = extract_resume_text(
            document_type, content, resolved_settings.max_pdf_pages,
            resolved_settings.max_pdf_decoded_stream_bytes, resolved_settings.max_extracted_text_chars,
        )
        sections = detect_resume_sections(text)
        try:
            analysis = ATSScorer(loaded_service, resolved_settings.scoring_weights).analyze(text, sections, job_description)
        except ModelLoadError as exc:
            raise IngestionError("model_not_ready", "The local scoring model is not ready.", 503) from exc
        analysis_id = None
        if authenticated is not None:
            user, token = authenticated
            database: SupabaseService = request.app.state.supabase_service
            record = await anyio.to_thread.run_sync(database.insert_analysis, user, token, analysis, sanitized_filename)
            analysis_id = record.get("id")
        return ResumeAnalysisResponse(filename=sanitized_filename, document_type=document_type, analysis=analysis, analysis_id=analysis_id)

    @app.post("/resumes/feedback", response_model=ResumeFeedbackResponse)
    async def resume_feedback(request: Request) -> ResumeFeedbackResponse:
        """Return unchanged deterministic analysis with separately generated Groq feedback."""
        authenticated = await optional_authenticated_user(request)
        feedback_client: GroqFeedbackService | None = getattr(request.app.state, "feedback_service", None)
        if feedback_client is None:
            raise FeedbackError("groq_not_configured", "Groq feedback is not configured.", 503)
        filename = _text_header(request, "x-upload-filename-base64", "x-upload-filename", "upload filename")
        raw_job_description = _text_header(request, "x-job-description-base64", "x-job-description", "job description")
        if not filename:
            raise IngestionError("missing_filename", "An X-Upload-Filename-Base64 header is required.", 400)
        if raw_job_description is None:
            raise IngestionError("missing_job_description", "An X-Job-Description-Base64 header is required.", 400)
        if len(raw_job_description) > resolved_settings.max_job_description_chars:
            raise IngestionError("job_description_too_large", "The job description exceeds the configured size limit.", 413)
        try:
            job_description = preprocess_job_description(raw_job_description)
        except ValueError as exc:
            raise IngestionError("unreadable_text", str(exc)) from exc
        loaded_service: LocalModelService | None = getattr(request.app.state, "model_service", None)
        if loaded_service is None or not loaded_service.is_ready:
            raise IngestionError("model_not_ready", "The local scoring model is not ready.", 503)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        validate_declared_content_length(request.headers.get("content-length"), resolved_settings.max_upload_bytes)
        content = await read_bounded_stream(request.stream(), resolved_settings.max_upload_bytes)
        sanitized_filename, document_type = validate_document(filename, content_type, content, resolved_settings.max_upload_bytes, resolved_settings.max_docx_zip_entries, resolved_settings.max_docx_total_uncompressed_bytes, resolved_settings.max_docx_entry_uncompressed_bytes)
        text = extract_resume_text(document_type, content, resolved_settings.max_pdf_pages, resolved_settings.max_pdf_decoded_stream_bytes, resolved_settings.max_extracted_text_chars)
        sections = detect_resume_sections(text)
        try:
            analysis = ATSScorer(loaded_service, resolved_settings.scoring_weights).analyze(text, sections, job_description)
        except ModelLoadError as exc:
            raise IngestionError("model_not_ready", "The local scoring model is not ready.", 503) from exc
        feedback = await anyio.to_thread.run_sync(feedback_client.generate, analysis)
        analysis_id = None
        if authenticated is not None:
            user, token = authenticated
            database: SupabaseService = request.app.state.supabase_service
            record = await anyio.to_thread.run_sync(database.insert_analysis, user, token, analysis, sanitized_filename, feedback)
            analysis_id = record.get("id")
        return ResumeFeedbackResponse(filename=sanitized_filename, document_type=document_type, analysis=analysis, feedback=feedback, analysis_id=analysis_id)

    @app.get("/analyses")
    async def list_analysis_history(authenticated: tuple[AuthenticatedUser, str] = Depends(require_authenticated_user), limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
        user, token = authenticated
        database: SupabaseService | None = getattr(app.state, "supabase_service", None)
        if database is None:
            raise HTTPException(status_code=503, detail="Analysis storage is not configured.")
        analyses = await anyio.to_thread.run_sync(database.list_analyses, user, token, limit, offset)
        return {"analyses": analyses, "limit": limit, "offset": offset}

    @app.get("/analyses/{analysis_id}")
    async def get_analysis(analysis_id: str, authenticated: tuple[AuthenticatedUser, str] = Depends(require_authenticated_user)) -> dict:
        try:
            from uuid import UUID
            record_id = UUID(analysis_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Analysis not found.") from exc
        user, token = authenticated
        database: SupabaseService | None = getattr(app.state, "supabase_service", None)
        if database is None:
            raise HTTPException(status_code=503, detail="Analysis storage is not configured.")
        record = await anyio.to_thread.run_sync(database.get_analysis, user, token, record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Analysis not found.")
        return record

    @app.delete("/analyses/{analysis_id}")
    async def delete_analysis(analysis_id: str, authenticated: tuple[AuthenticatedUser, str] = Depends(require_authenticated_user)) -> dict:
        try:
            from uuid import UUID
            record_id = UUID(analysis_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Analysis not found.") from exc
        user, token = authenticated
        database: SupabaseService | None = getattr(app.state, "supabase_service", None)
        if database is None:
            raise HTTPException(status_code=503, detail="Analysis storage is not configured.")
        deleted = await anyio.to_thread.run_sync(database.delete_analysis, user, token, record_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Analysis not found.")
        return {"deleted": True, "id": analysis_id}

    @app.post("/job-descriptions/preprocess", response_model=JobDescriptionResponse)
    async def preprocess_job_description_endpoint(
        request: JobDescriptionRequest,
    ) -> JobDescriptionResponse:
        """Normalize JD input locally; matching and scoring are intentionally deferred."""
        try:
            text = preprocess_job_description(request.text)
        except ValueError as exc:
            raise IngestionError("unreadable_text", str(exc)) from exc
        return JobDescriptionResponse(text=text, character_count=len(text))

    return app


app = create_app()
