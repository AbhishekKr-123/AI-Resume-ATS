"""Non-secret application configuration for the backend."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required local application resources are unavailable."""


@dataclass(frozen=True)
class Settings:
    """Configuration derived from the repository layout.

    The model location is deliberately fixed to the approved local artifact and
    is resolved from the project root rather than the process working directory.
    """

    project_root: Path
    model_relative_path: Path = Path("models/finetuned-bert")
    max_upload_bytes: int = 5 * 1024 * 1024
    max_pdf_pages: int = 20
    max_pdf_decoded_stream_bytes: int = 10 * 1024 * 1024
    max_extracted_text_chars: int = 200_000
    max_docx_zip_entries: int = 200
    max_docx_total_uncompressed_bytes: int = 20 * 1024 * 1024
    max_docx_entry_uncompressed_bytes: int = 5 * 1024 * 1024
    max_job_description_chars: int = 20_000
    semantic_weight: float = 35.0
    technical_skills_weight: float = 25.0
    keywords_weight: float = 15.0
    experience_relevance_weight: float = 15.0
    formatting_completeness_weight: float = 10.0
    groq_api_key: str | None = None
    groq_model: str | None = None
    groq_timeout_seconds: float = 20.0
    supabase_url: str | None = None
    supabase_publishable_key: str | None = None
    supabase_timeout_seconds: float = 15.0
    analysis_rate_limit_requests: int = 30
    analysis_rate_limit_window_seconds: float = 60.0

    @property
    def model_path(self) -> Path:
        return self.project_root / self.model_relative_path

    @property
    def scoring_weights(self) -> dict[str, float]:
        return {"semantic_similarity": self.semantic_weight, "technical_skills": self.technical_skills_weight, "keywords": self.keywords_weight, "experience_relevance": self.experience_relevance_weight, "formatting_completeness": self.formatting_completeness_weight}


def load_settings(project_root: Path | None = None) -> Settings:
    """Build and validate settings without loading environment secrets."""
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    load_dotenv(root / ".env", override=False)
    defaults = {
        "MAX_UPLOAD_BYTES": 5 * 1024 * 1024,
        "MAX_PDF_PAGES": 20,
        "MAX_PDF_DECODED_STREAM_BYTES": 10 * 1024 * 1024,
        "MAX_EXTRACTED_TEXT_CHARS": 200_000,
        "MAX_DOCX_ZIP_ENTRIES": 200,
        "MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES": 20 * 1024 * 1024,
        "MAX_DOCX_ENTRY_UNCOMPRESSED_BYTES": 5 * 1024 * 1024,
        "MAX_JOB_DESCRIPTION_CHARS": 20_000,
    }
    values: dict[str, int] = {}
    for name, default in defaults.items():
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be a positive integer.") from exc
        if value <= 0:
            raise ConfigurationError(f"{name} must be a positive integer.")
        values[name] = value

    if values["MAX_DOCX_ENTRY_UNCOMPRESSED_BYTES"] > values[
        "MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES"
    ]:
        raise ConfigurationError(
            "MAX_DOCX_ENTRY_UNCOMPRESSED_BYTES cannot exceed "
            "MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES."
        )

    weight_defaults = {"SEMANTIC_WEIGHT": 35.0, "TECHNICAL_SKILLS_WEIGHT": 25.0, "KEYWORDS_WEIGHT": 15.0, "EXPERIENCE_RELEVANCE_WEIGHT": 15.0, "FORMATTING_COMPLETENESS_WEIGHT": 10.0}
    weights: dict[str, float] = {}
    for name, default in weight_defaults.items():
        try:
            weight = float(os.getenv(name, str(default)))
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be a non-negative number.") from exc
        if weight < 0:
            raise ConfigurationError(f"{name} must be a non-negative number.")
        weights[name] = weight
    if abs(sum(weights.values()) - 100.0) > 0.0001:
        raise ConfigurationError("Scoring weights must sum to 100.")

    try:
        groq_timeout_seconds = float(os.getenv("GROQ_TIMEOUT_SECONDS", "20"))
    except ValueError as exc:
        raise ConfigurationError("GROQ_TIMEOUT_SECONDS must be a positive number.") from exc
    if groq_timeout_seconds <= 0:
        raise ConfigurationError("GROQ_TIMEOUT_SECONDS must be a positive number.")
    try:
        supabase_timeout_seconds = float(os.getenv("SUPABASE_TIMEOUT_SECONDS", "15"))
    except ValueError as exc:
        raise ConfigurationError("SUPABASE_TIMEOUT_SECONDS must be a positive number.") from exc
    if supabase_timeout_seconds <= 0:
        raise ConfigurationError("SUPABASE_TIMEOUT_SECONDS must be a positive number.")
    try:
        analysis_rate_limit_requests = int(os.getenv("ANALYSIS_RATE_LIMIT_REQUESTS", "30"))
        analysis_rate_limit_window_seconds = float(os.getenv("ANALYSIS_RATE_LIMIT_WINDOW_SECONDS", "60"))
    except ValueError as exc:
        raise ConfigurationError("Analysis rate-limit settings must be positive numbers.") from exc
    if analysis_rate_limit_requests <= 0 or analysis_rate_limit_window_seconds <= 0:
        raise ConfigurationError("Analysis rate-limit settings must be positive numbers.")

    settings = Settings(
        project_root=root,
        max_upload_bytes=values["MAX_UPLOAD_BYTES"],
        max_pdf_pages=values["MAX_PDF_PAGES"],
        max_pdf_decoded_stream_bytes=values["MAX_PDF_DECODED_STREAM_BYTES"],
        max_extracted_text_chars=values["MAX_EXTRACTED_TEXT_CHARS"],
        max_docx_zip_entries=values["MAX_DOCX_ZIP_ENTRIES"],
        max_docx_total_uncompressed_bytes=values["MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES"],
        max_docx_entry_uncompressed_bytes=values["MAX_DOCX_ENTRY_UNCOMPRESSED_BYTES"],
        max_job_description_chars=values["MAX_JOB_DESCRIPTION_CHARS"],
        semantic_weight=weights["SEMANTIC_WEIGHT"],
        technical_skills_weight=weights["TECHNICAL_SKILLS_WEIGHT"],
        keywords_weight=weights["KEYWORDS_WEIGHT"],
        experience_relevance_weight=weights["EXPERIENCE_RELEVANCE_WEIGHT"],
        formatting_completeness_weight=weights["FORMATTING_COMPLETENESS_WEIGHT"],
        groq_api_key=os.getenv("GROQ_API_KEY") or None,
        groq_model=os.getenv("GROQ_MODEL") or None,
        groq_timeout_seconds=groq_timeout_seconds,
        supabase_url=os.getenv("SUPABASE_URL") or None,
        supabase_publishable_key=os.getenv("SUPABASE_PUBLISHABLE_KEY") or None,
        supabase_timeout_seconds=supabase_timeout_seconds,
        analysis_rate_limit_requests=analysis_rate_limit_requests,
        analysis_rate_limit_window_seconds=analysis_rate_limit_window_seconds,
    )

    if not settings.model_path.is_dir():
        raise ConfigurationError(
            "Required local fine-tuned model directory is unavailable: "
            "models/finetuned-bert/."
        )

    if not (settings.model_path / "modules.json").is_file():
        raise ConfigurationError(
            "The local fine-tuned model is incomplete: modules.json is missing."
        )

    return settings
