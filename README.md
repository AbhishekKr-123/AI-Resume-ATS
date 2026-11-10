# AI Resume ATS

## Streamlit frontend (Phase 6)

The Streamlit interface lives in `frontend/`. It uses Supabase's **publishable**
key solely for signup/sign-in and keeps the resulting access token only in
Streamlit's server-side session state. It never uses the Supabase secret key or
Groq API key. When signed in, the token is passed to the existing backend as a
Bearer token, allowing the existing RLS policies to scope saved history.

### Windows setup and launch

From the project root, install dependencies, then use two PowerShell windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```powershell
# Terminal 1: FastAPI (loads the local fine-tuned model)
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

```powershell
# Terminal 2: Streamlit
.\.venv\Scripts\python.exe -m streamlit run frontend/app.py
```

Set `API_BASE_URL=http://127.0.0.1:8000`, `SUPABASE_URL`, and
`SUPABASE_PUBLISHABLE_KEY` in your local `.env`. Do not place the Supabase
secret key or Groq key in frontend configuration or browser-visible code.

### Using the frontend

1. Create an account or sign in. You may continue as a guest for stateless ATS
   scoring.
2. Open **Analyze resume**, upload a PDF/DOCX (up to the configured 5 MiB
   default), paste a job description, and select **Analyze resume**.
3. Choose **Generate AI feedback** only when feedback is needed; it calls the
   optional Groq endpoint and displays its actual structured response.
4. Signed-in users can review and delete only their own persisted analyses in
   **Analysis history**. Guest analyses are not saved.

The frontend sends the original document bytes as a raw request body, with
`Content-Type`, `X-Upload-Filename`, and `X-Job-Description` headers required
by the backend. It does not use multipart upload. ATS scores are deterministic
alignment indicators, not hiring probabilities.

### Security controls and limits

- FastAPI verifies supplied Supabase bearer tokens server-side and derives the
  user identity from Supabase Auth; user IDs from a client are never trusted.
- The SQL migration enables RLS and limits analysis SELECT, INSERT, and DELETE
  actions to the authenticated row owner.
- PDF/DOCX requests use bounded raw-body streaming, MIME/signature checks, PDF
  page/resource limits, and DOCX archive limits. No original document, complete
  extracted resume, or complete job description is stored.
- Analysis and feedback endpoints have a configurable process-local rate limit
  (`ANALYSIS_RATE_LIMIT_REQUESTS`, default 30 per
  `ANALYSIS_RATE_LIMIT_WINDOW_SECONDS`, default 60). For multiple backend
  workers/instances, enforce a shared rate limit at the reverse proxy or API
  gateway; the built-in limiter is intentionally not distributed.
- Frontend errors are mapped to safe user messages. Resume/JD input is never
  rendered as unsafe HTML, and access tokens are held only in Streamlit session
  state.

## Backend setup

From the project root, install the pinned dependencies and start the API:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

The local fine-tuned model is loaded once during application startup from
`models/finetuned-bert/`. The application does not fall back to another model.

Set the documented Phase 2 environment variables before starting the server.
Defaults are listed in `.env.example`; `MAX_UPLOAD_BYTES` defaults to `5242880`
(5 MiB). If using an `.env` file, the deployment process must load it into the
environment before starting Uvicorn.

## API endpoints

### Health checks

- `GET /health/live` — process liveness.
- `GET /health/ready` — local model readiness and embedding dimension.

### Resume extraction

`POST /resumes/extract` accepts a **raw PDF or DOCX request body**. It does not
accept multipart form data. Send the content type as `Content-Type` and supply
the original filename only through `X-Upload-Filename`; the server sanitizes it
and uses it solely for file-type validation and response metadata.

- Supported formats: PDF (`application/pdf`) and DOCX
  (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`).
- The API streams request bytes into a bounded in-memory buffer, rejecting an
  upload as soon as it exceeds `MAX_UPLOAD_BYTES`. The application does not use
  `UploadFile`, multipart file spooling, or application-managed temporary files.
- A declared `Content-Length` above the limit is rejected before application
  streaming. The stream check remains authoritative for chunked or dishonest
  requests. The application retains no more than `MAX_UPLOAD_BYTES` of received
  request data, but materializing immutable bytes for parsing can temporarily
  duplicate that buffer (up to roughly `2 × MAX_UPLOAD_BYTES`, before parser
  allocations). The ASGI server may also allocate one received request chunk
  before the application can inspect it, so configure server/proxy request-body
  limits as well.
- The API validates extension, declared MIME type, file signature, PDF page/text
  limits, pypdf decoded-stream limits, DOCX ZIP structure/entry/uncompressed-size
  limits, and extracted-text limits before returning a result.
- The application does not persist, log, or transmit uploaded documents or
  extracted text. This is an application-level guarantee only: ASGI servers,
  reverse proxies, host monitoring, and request/response logging must be
  configured separately to meet an end-to-end disk-free/privacy requirement.
- The response contains sanitized filename metadata, extracted text, character
  count, and detected basic resume sections.

```powershell
curl.exe -X POST http://127.0.0.1:8000/resumes/extract `
  -H "Content-Type: application/pdf" `
  -H "X-Upload-Filename: synthetic-resume.pdf" `
  --data-binary "@C:\path\to\synthetic-resume.pdf"
```

Error responses have the form:

```json
{"error":{"code":"document_limit_exceeded","message":"The PDF exceeds the configured page limit."}}
```

### Job-description preprocessing

`POST /job-descriptions/preprocess` normalizes whitespace locally for a future
matching phase. It does not score, transmit, or persist the text.

```powershell
curl.exe -X POST http://127.0.0.1:8000/job-descriptions/preprocess `
  -H "Content-Type: application/json" `
  -d '{"text":"Senior Engineer\nBuild reliable APIs"}'
```

### Deterministic ATS analysis

`POST /resumes/analyze` uses the same raw document body and required
`X-Upload-Filename` header as extraction. Supply the job description in the
`X-Job-Description` header. This endpoint is local-only and does not use Groq,
external embedding services, or persistent storage.
`X-Job-Description` is capped by `MAX_JOB_DESCRIPTION_CHARS` (default 20,000).

```powershell
curl.exe -X POST http://127.0.0.1:8000/resumes/analyze `
  -H "Content-Type: application/pdf" `
  -H "X-Upload-Filename: synthetic-resume.pdf" `
  -H "X-Job-Description: Python and SQL required. Docker preferred." `
  --data-binary "@C:\path\to\synthetic-resume.pdf"
```

The score is out of 100:

| Component | Default weight | Formula |
|---|---:|---|
| Semantic similarity | 35% | Token-weighted mean of each unique JD chunk's best local-model cosine match. |
| Technical skills | 25% | 80% required-skill coverage + 20% preferred-skill coverage. |
| Keywords | 15% | Weighted unique JD-term coverage; recognized technical phrases have 3× weight. |
| Experience relevance | 15% | Work/project term relevance; explicit-duration coverage is 30% only when the JD and resume both state years. |
| Formatting/completeness | 10% | Contact, summary, education, skills, work-or-project evidence, and readability checks. |

The weighted total is `sum(component_score × component_weight / 100)`.
Weights are configured through `SEMANTIC_WEIGHT`, `TECHNICAL_SKILLS_WEIGHT`,
`KEYWORDS_WEIGHT`, `EXPERIENCE_RELEVANCE_WEIGHT`, and
`FORMATTING_COMPLETENESS_WEIGHT`; they must be non-negative and total 100.

Semantic matching uses only the already-loaded local fine-tuned model at
`models/finetuned-bert/`. Text is section-aware chunked with the model's
384-token maximum; duplicate chunks are removed before comparison.

Example response shape:

```json
{
  "filename": "synthetic-resume.pdf",
  "document_type": "pdf",
  "analysis": {
    "overall_ats_score": 82.5,
    "maximum_score": 100,
    "methodology_version": "deterministic-ats-v1",
    "components": {"semantic_similarity": {"score": 80, "weight": 35}},
    "matched_skills": ["python", "sql"],
    "missing_skills": ["docker"],
    "areas_needing_improvement": []
  }
}
```

Scores are heuristic evidence indicators only. They are not hiring probabilities,
do not guarantee selection, and should not be used as the sole employment decision.
Section detection, aliases, keyword heuristics, extraction quality, and explicit
experience statements can all affect results. Projects can satisfy the
experience/project completeness check for freshers.

### Groq feedback

`POST /resumes/feedback` accepts the same raw document body and headers as
`/resumes/analyze`. It first calculates the unchanged deterministic analysis,
then sends only limited scoring evidence to Groq for recommendations.

Configure these server-side environment variables (never send them from a
client):

```text
GROQ_API_KEY=...
GROQ_MODEL=...
GROQ_TIMEOUT_SECONDS=20
```

The Groq client is created once during application startup when both key and
model are configured. It is not created per request. If configuration is absent
or Groq fails, `/resumes/feedback` returns a structured error; `/resumes/analyze`
continues to work without Groq.

```powershell
curl.exe -X POST http://127.0.0.1:8000/resumes/feedback `
  -H "Content-Type: application/pdf" `
  -H "X-Upload-Filename: synthetic-resume.pdf" `
  -H "X-Job-Description: Python and SQL required. Docker preferred." `
  --data-binary "@C:\path\to\synthetic-resume.pdf"
```

The feedback payload has `overall_feedback`, `strengths`,
`missing_skills_feedback`, `resume_improvements`, `project_recommendations`,
and a prioritized `action_plan`. Groq output must validate against a Pydantic
schema and is rejected when it cites unsupported evidence, invents a missing
skill, or recommends project skills outside the documented gaps.

The prompt includes deterministic scores, matched/missing skills and keywords,
section labels, experience findings, formatting checks, and controlled evidence
labels. It deliberately excludes contact information and full resume/JD text.
Do not configure infrastructure to log request headers, because the job
description is supplied in `X-Job-Description`.

Typical feedback errors include `groq_not_configured` (503),
`groq_authentication_failed` (502), `groq_rate_limited` (429), `groq_timeout`
(504), `groq_unavailable` (503), and `invalid_feedback` (502).

## Supabase authentication and analysis history

Phase 5 adds optional authenticated persistence. The backend verifies a supplied
Supabase access token with Supabase Auth's `/auth/v1/user` endpoint. It derives
the user ID only from that verified response; request bodies and custom user-ID
headers are never trusted.

When `Authorization` is absent, `/resumes/analyze` and `/resumes/feedback`
remain stateless. When it contains a valid bearer token, the resulting analysis
is saved under that authenticated user and responses include `analysis_id`. An
invalid supplied token returns `401`; it is never silently ignored.

The backend uses `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and the user access
token for every user-owned database request. This preserves PostgreSQL RLS.
`SUPABASE_SECRET_KEY` is not used for normal CRUD and must never be exposed to
Streamlit or other clients.

### Manual database setup

1. Open the Supabase project's **SQL Editor**.
2. Run [`backend/sql/001_create_analyses_table.sql`](backend/sql/001_create_analyses_table.sql).
3. Verify that RLS is enabled for `public.analyses` and that the three policies
   were created.

The migration creates an `analyses` table with score/evidence JSON, optional
structured feedback, sanitized filename metadata, user ID, and timestamp. It
does **not** store original documents, extracted resume text, JD text, contact
information, tokens, or API keys. The migration has not been run automatically
by this application.

### Protected endpoints

- `GET /analyses?limit=20&offset=0` — authenticated user's newest analyses.
- `GET /analyses/{analysis_id}` — one authenticated user's analysis.
- `DELETE /analyses/{analysis_id}` — deletes one authenticated user's analysis.

```powershell
curl.exe http://127.0.0.1:8000/analyses `
  -H "Authorization: Bearer <SUPABASE_ACCESS_TOKEN>"
```

```powershell
curl.exe -X DELETE http://127.0.0.1:8000/analyses/<ANALYSIS_UUID> `
  -H "Authorization: Bearer <SUPABASE_ACCESS_TOKEN>"
```

RLS additionally enforces `user_id = auth.uid()` for SELECT, INSERT, and
DELETE. The API also filters by the verified user ID so it does not reveal
whether another user's analysis exists.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m compileall -q backend tests
```
