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

## Startup/import repair and end-to-end audit (2026-11-11)

### Confirmed causes

- There is exactly one `ApiClient` definition:
  `frontend/api_client.py::ApiClient`. It contains `health_live()` and
  `health_ready()` as instance methods using the existing `_request` timeout,
  error, and JSON handling. Both invoke only `/health/live` and
  `/health/ready` respectively. The reported missing-method exception cannot
  be reproduced from the current source; it is consistent with an older running
  Streamlit worker or a prior source version being served.
- `streamlit run frontend/app.py` executes `app.py` as a script. Depending on
  the runner path setup, Python can include `frontend/` but omit its parent,
  which makes absolute imports such as `from frontend.api_client ...` fail.
  `frontend/app.py` now resolves the project root and inserts it into `sys.path`
  before importing the frontend package. This provides one canonical package
  path and avoids switching between script-relative and package imports.
- No application source requests bare `/health`; continued bare-path 404s must
  be caused by an external tab, proxy, or monitor configured with that URL.
  During the live Uvicorn check, loopback requests to `/health` continued while
  the project verification calls correctly reached `/health/live` and
  `/health/ready`, confirming this is not emitted by the project source.

### Actual verification

```powershell
# From AI-Resume-ATS
.\.venv\Scripts\python.exe -c "import frontend, frontend.api_client, frontend.auth, frontend.app ..."
# Result: package imports and health methods succeeded
# ApiClient resolved from C:\ML-Project\AI-Resume-Workspace\AI-Resume-ATS\frontend\api_client.py
# Result: health_live=True; health_ready=True

.\.venv\Scripts\python.exe -c "import runpy; runpy.run_path('frontend/app.py', run_name='script_import_check') ..."
# Result: script-mode frontend imports succeeded

Invoke-RestMethod http://127.0.0.1:8000/health/live
# Result: {"status":"alive"}
Invoke-RestMethod http://127.0.0.1:8000/health/ready
# Result: {"status":"ready","model":"local-finetuned","embedding_dimension":768}

.\.venv\Scripts\python.exe -c "... ApiClient('http://127.0.0.1:8000').analyze(...)"
# Result: analysis_api_ok docx 83.22

.\.venv\Scripts\streamlit.exe run frontend/app.py --server.headless true --server.port 8501
# Result: app served as "AI Resume ATS" with no browser console errors or startup traceback

.\.venv\Scripts\python.exe -m pytest tests -q
# Result: 71 passed in 61.43s

.\.venv\Scripts\python.exe -m compileall -q backend frontend tests
# Result: Compilation completed successfully.
```

The feedback-unavailable behavior, Supabase authentication, database/RLS, and
Groq paths remain mock-tested only; no live credentials or external provider
requests were used in this audit.

## Targeted Supabase signup repair (2026-11-11)

### Production-log root cause

`frontend/auth.py` passes only the `publishable_key` constructor argument to
`supabase.create_client`. `frontend/config.py` previously filled that argument
from `SUPABASE_PUBLISHABLE_KEY` without checking its format. A non-secret local
configuration check found `supabase_url_configured=True`,
`publishable_key_available=False`, and
`secret_key_misconfiguration_detected=True`. Combined with the production log's
`sb_secret_` request-key prefixes, this confirms that a Supabase secret key was
put in `SUPABASE_PUBLISHABLE_KEY`. The frontend had no alternate secret-key
selection path.

The `email_address_invalid` 400 is a Supabase validation rejection. The
provider log does not safely expose the submitted address, so its exact input
defect cannot be determined from the log alone. Before this repair, the UI
trimmed surrounding whitespace and applied a basic UI regex, but direct wrapper
calls could bypass that validation. Email is now normalized and validated at the
auth-client boundary before signup, login, or password reset; invalid input is
rejected locally without an Auth request.

`over_email_send_rate_limit` is a Supabase email-delivery limit, not an
application retry failure. The frontend makes no automatic retries and now
shows a safe wait message. It requires waiting for the Supabase window to reset
or configuring custom SMTP/email settings in the dashboard; it cannot safely be
bypassed in code.

### Changes

- `frontend/config.py` now reads only the allowed frontend configuration names
  from `.env` with `dotenv_values`, instead of loading all server-only dotenv
  values into the Streamlit process environment. It never reads
  `SUPABASE_SECRET_KEY`.
- `sb_secret_` values assigned to `SUPABASE_PUBLISHABLE_KEY` are blocked and
  produce a safe configuration error rather than reaching Supabase Auth.
- `SupabaseAuth` independently rejects a secret key, protecting direct wrapper
  construction as well.
- Signup, login, and password reset use the same trimmed, validated email.
- Safe provider-code handling now covers `email_address_invalid` and
  `over_email_send_rate_limit` without exposing provider details or retrying.
- `.env.example` and `README.md` document the required publishable-key format
  and manual Supabase Email/SMTP configuration.

### Verification

```text
Focused frontend tests: 28 passed in 3.27s
Full pytest suite: 81 passed in 64.22s
python -m compileall -q backend frontend tests: succeeded
Mocked provider-code handling: succeeded
```

The repair intentionally did not attempt a live signup while the provider rate
limit is active. After replacing the locally misconfigured key, wait for the
limit to reset before performing one manual signup test. Live Supabase email
delivery, confirmation, reset, and RLS behavior remain unverified.

## Google OAuth (2026-11-11)

### Implementation

Google OAuth was added without removing email/password authentication. Removing
the existing path would break current accounts and was not necessary for OAuth.

- `SupabaseAuth.google_authorization_url()` calls Supabase
  `sign_in_with_oauth` using provider `google` and the configured redirect URL.
- The Streamlit sign-in page renders a **Continue with Google** link button.
- On callback, `_consume_google_callback` reads only the one-time `code`, calls
  `exchange_code_for_session`, verifies the resulting identity through
  `get_user(access_token)`, then stores the access token and verified email only
  in Streamlit session state.
- Callback errors/cancellation produce a safe message. The one-time code and
  provider error parameters are cleared from `st.query_params` immediately,
  avoiding persistence in the visible URL or logs.
- Logout uses the existing Supabase sign-out plus session cleanup; existing
  backend bearer verification and RLS continue to apply to OAuth-issued tokens.

`FRONTEND_REDIRECT_URL` was added to `.env.example`, defaulting to
`http://localhost:8501`. It must exactly match an allowed Supabase redirect
URL. OAuth client ID/secret are managed solely by Google Cloud and Supabase,
not by this codebase.

### Verification

```text
Focused frontend OAuth/auth tests: 30 passed in 5.26s
Full pytest suite: 83 passed in 66.90s
python -m compileall -q backend frontend tests: succeeded
OAuth wrapper import and methods: succeeded
Streamlit startup: served AI Resume ATS on port 8501 with no browser console errors
```

Mock tests verify a publishable-key client starts Google OAuth with the exact
redirect URL, exchanges the authorization code, verifies the returned user, and
maps callback failures safely. A live Google login was not run: the local
`SUPABASE_PUBLISHABLE_KEY` remains deliberately blocked as an `sb_secret_`
misconfiguration from the preceding repair. Replace it with the project
publishable key and wait for any Supabase email rate limit to reset before
performing one manual OAuth test. Live OAuth logout, persistence, and RLS
history verification therefore remain pending.

## Google OAuth callback diagnostics (2026-11-11)

The generic UI message originates in `SupabaseAuth.complete_google_callback`:
it catches the exception from `exchange_code_for_session`, maps it to a safe
`AuthError`, and the Streamlit callback displays that safe message. The original
provider exception was previously hidden completely.

The callback now writes two safe terminal diagnostics:

- callback query **parameter names** only (for example `['code']` or
  `['error', 'error_description']`), never their values;
- the authorization-code exchange exception class and a redacted message.

Redaction removes values associated with codes, tokens, authorization headers,
keys, and secrets, and masks JWT-shaped strings. No authorization code, access
token, refresh token, OAuth client secret, or API key is logged. The existing
safe UI error remains unchanged.

To reproduce, start Streamlit from PowerShell, select **Continue with Google**,
complete account selection, then inspect the same Streamlit terminal. Look for:

```text
Google OAuth callback received query parameter names: ...
Google OAuth callback received an authorization code; beginning Supabase exchange.
Supabase OAuth authorization-code exchange failed (<ExceptionType>): <redacted message>
```

The final line distinguishes causes such as a missing PKCE verifier, expired or
reused authorization code, redirect mismatch, or provider rejection without
leaking credentials. Focused frontend diagnostics tests: `31 passed in 3.22s`;
frontend/test compilation succeeded. A live OAuth exchange was not retried
because the local publishable-key misconfiguration remains unresolved.

## Google OAuth PKCE verifier repair (2026-11-11)

### Exact root cause

The installed `supabase==2.34.1` defaults to PKCE. Its
`sign_in_with_oauth()` calls `_prepare_pkce_challenge()`, generates a verifier,
and writes it to the SDK client's configured storage under a key ending in
`-code-verifier`. `exchange_code_for_session()` later reads that storage unless
a verifier is supplied explicitly.

The application recreated `SupabaseAuth` and its Supabase client on the OAuth
callback rerun. Each client used the SDK's default `SyncMemoryStorage`, so the
callback client had an empty store. Supabase consequently received the auth code
with no verifier and returned: `both auth code and code verifier should be
non-empty`.

### Repair

- Added `frontend/oauth_storage.py`, a thread-safe, process-local PKCE store.
- Each OAuth start receives a cryptographically random opaque flow ID. The SDK
  client that generates the PKCE challenge uses `OAuthFlowStorage(flow_id)`.
- The callback URL contains only that opaque ID as `oauth_flow`; the verifier
  remains server-side and expires after 10 minutes.
- Callback handling atomically consumes the verifier and passes it explicitly
  to `exchange_code_for_session`. A flow can therefore be exchanged only once;
  repeated callbacks stop before a Supabase request.
- The storage adapter ignores SDK session writes, so access/refresh tokens are
  not retained in the PKCE store. Existing access-token handling remains in
  Streamlit session state.
- Missing, expired, already-consumed, or process-restart-invalidated flows show
  a safe "start Google sign-in again" message. Existing redacted diagnostic
  logging is preserved and never logs the verifier, code, or tokens.

This keeps PKCE enabled and does not switch to implicit flow. Pending local
flows intentionally do not survive a Streamlit process restart or a deployment
to a different worker. A multi-worker deployment should replace this in-memory
store with a shared server-side TTL store while preserving the same interface.

### Verification

```text
Focused frontend tests: 33 passed in 3.41s
Full pytest suite: 86 passed in 49.96s
supabase=2.34.1
PKCE rerun persistence and single-use consumption: verified
python -m compileall -q backend frontend tests: succeeded
```

Regression tests cover storage recreation across reruns, successful exchange
with an explicit persisted verifier, missing verifier, single-use/repeated
callback rejection, verified user creation, and redacted error diagnostics.
The full suite also passed. A manual live Google OAuth test is still required
after configuring a valid
`SUPABASE_PUBLISHABLE_KEY`.

## Unicode-safe analysis headers

The Streamlit client previously placed the job description directly in
`X-Job-Description`. HTTPX encodes header values as ASCII, so punctuation such
as an em dash (`U+2014`), accented text, and other Unicode caused a
`UnicodeEncodeError` before the request reached FastAPI. The upload filename had
the same latent issue.

- `frontend/api_client.py` now UTF-8 encodes filenames and job descriptions,
  then URL-safe Base64 encodes them into ASCII-only
  `X-Upload-Filename-Base64` and `X-Job-Description-Base64` headers for both
  analysis and feedback requests.
- `backend/app/main.py` strictly decodes URL-safe Base64 and UTF-8 before
  validation/preprocessing on extraction, analysis, and feedback endpoints.
  Malformed values return a safe `invalid_header_encoding` 400 response.
- Existing ASCII `X-Upload-Filename` and `X-Job-Description` headers remain as
  a compatibility fallback; the frontend no longer sends them.
- Decoded job-description length limits, document validation, scoring,
  authentication, persistence, and feedback behavior are unchanged. Neither
  encoded nor decoded values are logged.

Tests cover em dashes, accented French text, Japanese text, Unicode filenames,
both `/resumes/analyze` and `/resumes/feedback`, malformed Base64, and the
absence of raw Unicode headers. Existing secure filename sanitization remains
active after decoding (for example, a Unicode filename is normalized to safe
response metadata).

```text
Focused frontend/scoring/feedback tests: 67 passed in 39.70s
Full pytest suite: 95 passed in 60.47s
python -m compileall -q backend frontend tests: succeeded
```
