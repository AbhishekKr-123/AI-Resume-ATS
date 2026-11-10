# Phase 6: Streamlit frontend

## Created files

- `frontend/app.py`
- `frontend/config.py`
- `frontend/api_client.py`
- `frontend/auth.py`
- `frontend/components/score_display.py`
- `frontend/components/feedback_display.py`
- `frontend/__init__.py`
- `tests/test_frontend.py`
- `tests/test_rate_limit.py`
- `backend/app/rate_limit.py`

## Modified files

- `requirements.txt`
- `.env.example`
- `README.md`
- `backend/app/feedback_service.py`
- `tests/test_feedback.py`
- `backend/app/config.py`
- `backend/app/main.py`
- `frontend/app.py`
- `frontend/auth.py`

## Features

- Supabase signup, login, logout, and session-only access-token handling.
- Guest, stateless deterministic analysis.
- Raw PDF/DOCX upload calls to `/resumes/analyze` and `/resumes/feedback` with
  the required headers and MIME types; no multipart form upload.
- ATS score, five component score displays, evidence, matched/missing terms,
  disclaimer, and actual validated feedback display.
- Authenticated dashboard, history detail lookup, pagination, and confirmed
  deletion via existing RLS-protected backend endpoints.
- User-friendly backend, authentication, Groq, validation, and timeout errors.

## API integration

`ApiClient` uses HTTPX with a 90-second timeout. It sends an Authorization
Bearer header only when a session token exists. It passes raw document bytes and
does not alter backend scoring, persistence, model, or RLS behavior.

Persisted history responses store component columns rather than the full
transient analysis payload. The UI maps only those returned fields for historic
result display and intentionally leaves unavailable evidence lists empty.

## Security

- Only `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `API_BASE_URL` are read
  by frontend code.
- Access tokens are held in Streamlit session state; they are not written to
  browser storage or files and are cleared on logout/401.
- The secret/service key and Groq key are never exposed to the frontend.
- The backend remains the authorization boundary for history and RLS remains
  enforced by its existing authenticated REST requests.

## Commands

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe -m streamlit run frontend/app.py
```

## Limitations

Tests mock FastAPI, Supabase Auth, and Groq-facing paths. No live Supabase,
live RLS, or real Groq request was run. Streamlit session state is server-side
for the active session; users must sign in again after a new session.

## Verification

```text
python -m pytest tests -q
69 passed in 47.41s

python -m compileall -q backend frontend tests
Compilation completed successfully.

python -c "import frontend.app; from frontend.api_client import ApiClient"
Frontend imports succeeded.

python -c "from backend.app.feedback_service import feedback_response_format"
Structured feedback schema import succeeded.
```

`pip install -r requirements.txt` also completed using the updated dependency
file. Streamlit started headlessly on port 8502 and served the `AI Resume ATS`
page without browser console errors. This does not exercise a live Supabase or
Groq integration.

## Structured Groq feedback fix

The feedback service now requests Groq Structured Outputs rather than JSON
Object Mode. Its `response_format` uses `type: json_schema`, name
`resume_feedback`, `strict: false`, and the schema generated directly from
`FeedbackResponse.model_json_schema()`. This covers the six required top-level
fields and all nested Pydantic models. The response is still validated by
Pydantic and then subjected to the existing evidence and missing-skill grounding
checks; no fields are fabricated or defaulted when Groq returns an invalid
response.

Mocked tests cover valid structured output, missing fields, invalid field types,
unsupported evidence, unsupported missing skills, and Groq API failures.

## Project-recommendation grounding fix

Project recommendation skills are now authorized independently from missing-skill
feedback. The service normalizes skills with trimmed case-folding and permits a
project recommendation only when every demonstrated skill is in the supplied
matched or missing skill lists. Missing-skill feedback still uses the separate,
strict missing required/preferred map and requires its exact supplied
importance. Focused feedback tests: `17 passed in 32.03s`.

## Final Phase 6 improvements and security review

### Groq grounding

The Groq payload now includes `allowed_project_skills` (normalized matched plus
missing skills). The system prompt requires exact supplied names only, forbids
synonyms/related/invented skills, and explicitly allows an empty project-skill
list. Runtime validation remains case-insensitive and whitespace-normalized.
Missing-skill required/preferred validation is unchanged.

### UI and functional workflows

The Streamlit interface was redesigned with an accessible light SaaS visual
system, branded authentication, meaningful dashboard metrics, persistent
navigation, upload filename/size/remove controls, job-description count,
analysis/feedback loading states, history detail/delete confirmation, and an
authenticated profile page. Dashboard metrics and history use only actual API
responses. Guest mode is explicitly limited to stateless analysis.

Buttons are connected to their real handlers: sign-in, signup, reset-password,
guest access, navigation, remove upload, analyze, feedback, history detail,
delete confirmation/pagination, profile reset, and logout. Tests cover client
request headers/raw bytes, document validation, API failures, session clearing,
and safe auth-message classification. Streamlit interaction flows were not
automated browser tests; they require a configured Supabase project for live
authentication verification.

### Authentication and security hardening

Safe Supabase error classification distinguishes invalid credentials,
unconfirmed email, duplicate account, weak password, and service/network errors
without exposing provider payloads. The existing backend bearer verification,
RLS migration, raw bounded ingestion, signature/archive/PDF protections, user
scoped queries, publishable-key-only frontend, and secret-free persistence were
reviewed and retained.

A dependency-free, thread-safe process-local rate limiter now protects the
expensive `/resumes/analyze` and `/resumes/feedback` routes (default 30 per
client per 60 seconds). It returns a safe 429 response without consuming the
request body. It is not a distributed limiter; production multi-instance
deployments need a shared proxy/gateway limiter. CORS is not configured because
the Streamlit server calls FastAPI server-to-server; if a browser SPA is later
introduced, use an explicit origin allow-list rather than a wildcard.

### Final verification and limits

```text
Focused feedback/frontend/rate-limit tests: 35 passed in 24.52s
Full pytest suite: 69 passed in 47.41s
python -m compileall -q backend frontend tests: succeeded
Backend and frontend imports: succeeded
Uvicorn startup + /health/live + /health/ready: succeeded
Streamlit headless startup on port 8503: succeeded with no browser console errors
```

All Supabase and Groq tests are mocked. No live Supabase authentication, email
confirmation/reset delivery, RLS database policy execution, or Groq call was
performed. The migration must still be applied manually in Supabase, and live
deployment needs an upstream distributed rate limit plus HTTPS/secret handling.

## Repeated `GET /health` 404 diagnosis and fix

### Root cause

A full-project audit (all source, config, notebook, and script files) found no
component that ever requests the bare `/health` URL. The backend only defines
`/health/live` and `/health/ready`, and the only references to those in the
repository were the backend routes and their own tests. The repeated
`GET /health 404` entries therefore come from an external requester—most
commonly a browser tab or monitor pointing directly at
`http://127.0.0.1:8000/health` and auto-refreshing—not from the application
code.

### Fix

Instead of adding a compat `/health` alias to hide the symptom, the frontend now
performs explicit, correct health checking through the existing API client:

- `ApiClient.health_live()` calls `GET /health/live` (liveness).
- `ApiClient.health_ready()` calls `GET /health/ready` (model readiness).
- `frontend/app.py::check_backend` probes both and reports liveness, model
  readiness, and the ready model name.
- The sidebar shows a cached `Backend: Online/Offline · Model ready` line using
  `st.cache_data(ttl=60)`, so Streamlit reruns re-issue health requests at most
  once per minute, with a manual "Check status" button that busts the cache on
  demand. There is no polling loop and no hiding of genuine backend failures:
  an offline backend shows as Offline.

### Verification

```text
python -m pytest tests/frontend-focused run: 18 passed in 3.67s
Full pytest suite: 71 passed in 52.76s
python -m compileall -q backend frontend tests: succeeded
```

New mocked tests assert the client uses exactly the two existing endpoint paths
and never the bare `/health` path, plus outage and partial-readiness handling.
