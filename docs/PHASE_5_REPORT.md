# Phase 5: Supabase database and authentication

## Implemented files

- `backend/sql/001_create_analyses_table.sql`
- `backend/app/database_service.py`
- `backend/app/auth.py`
- `tests/test_supabase_integration.py`

Modified: `backend/app/config.py`, `backend/app/main.py`, `.env.example`, and
`README.md`.

## Database and RLS

The `analyses` table stores only user ID, numeric score, structured component
evidence, matched/missing lists, optional feedback, sanitized filename, and
timestamp. It has user and user/timestamp indexes, an `auth.users` foreign key
with cascade deletion, score constraints, RLS, and own-row SELECT/INSERT/DELETE
policies. It has no UPDATE policy.

The supplied migration must be run manually in Supabase SQL Editor. This phase
did not access the dashboard or apply the migration to a live project.

## Authentication and persistence

`Authorization: Bearer <token>` is verified through Supabase Auth's user
endpoint. The verified UUID is the only user identity used. The reusable
Supabase REST client uses the publishable key plus the current user's token for
all CRUD so RLS remains active. The secret key is not used for normal CRUD.

Unauthenticated analysis/feedback remains stateless. Authenticated analysis and
feedback persist once and return `analysis_id`. Storage failure returns a safe
error rather than claiming a save succeeded.

## Endpoints

- `GET /analyses`
- `GET /analyses/{analysis_id}`
- `DELETE /analyses/{analysis_id}`

All require verified authentication. Cross-user reads/deletes return 404.

## Verification

Automated tests mock Supabase/Auth; they do not contact a live project or
verify live RLS enforcement. Run the migration, create two test users, obtain
their access tokens, then verify one user cannot list/get/delete the other's
record.

```text
python -m pytest tests -v
44 passed in 54.86s

python -m compileall -q backend tests
Compilation completed successfully.
```

## Security decisions and limitations

- No documents, full resume/JD text, contact details, or secrets are persisted.
- Errors do not include SQL, token, or Supabase response details.
- HTTP calls use configured timeouts.
- Live Supabase connectivity/RLS requires manual verification after migration.
