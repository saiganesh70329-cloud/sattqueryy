# SATQUERY

SATQUERY is a React/Vite frontend and FastAPI backend for satellite-image analysis demonstrations. It preserves user authentication and roles, land classification, change detection, flood screening, history, notifications, admin controls, original-image retrieval, and PDF reports. The analysis methods are heuristic demonstrations, not trained models or verified live satellite products.

## Local development

Requirements: Node.js 18+ and Python 3.10+.

In one terminal:

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
# Set a private local SECRET_KEY and ADMIN_PASSWORD.
uvicorn app.main:app --reload --port 8002
```

In a second terminal from the repository root:

```powershell
npm install
npm run dev
```

Open http://localhost:5173. The frontend uses `VITE_API_BASE_URL` when set; otherwise local development uses `http://127.0.0.1:8002/api`. The backend uses local SQLite by default. Keep `.env` files private.

## Vercel Services architecture

The root `vercel.json` declares two services in one Vercel project:

| Service | Root | Framework | Public route |
| --- | --- | --- | --- |
| `app` | `.` | Vite | `/` and frontend routes |
| `backend` | `backend` | FastAPI | `/api/*` |

The ordered rewrites route `/api/*` to FastAPI and all other paths to the Vite frontend. Select **Services** as the Vercel project framework and confirm the account has access to Vercel Services. No service bindings are needed: the browser calls the public same-domain `/api` path and the backend does not call another Vercel service. Vercel Services availability and framework support must be verified in the target account before deployment.

In production, the frontend uses relative `/api` requests. `VITE_API_BASE_URL` is only a local-development override; do not place secrets in any `VITE_` variable. FastAPI routes already include `/api` once.

## Production environment variables

Set these on the Vercel `backend` service only, for the appropriate preview/production environments. Never commit actual values:

- `APP_ENV=production`
- `DATABASE_URL` — PostgreSQL URL. PostgreSQL is mandatory in production; SQLite remains for local development.
- `SECRET_KEY` — a newly generated private signing key.
- `ADMIN_EMAIL` and `ADMIN_PASSWORD` — private bootstrap/admin settings. Keep `ADMIN_BOOTSTRAP_ENABLED=false` when importing existing users.
- `FRONTEND_ORIGINS` — exact HTTPS origins for the deployed frontend, comma-separated and without paths or wildcards.
- `OBJECT_STORAGE_ENDPOINT_URL` — HTTPS S3-compatible object-storage endpoint.
- `OBJECT_STORAGE_BUCKET` — private bucket name.
- `OBJECT_STORAGE_REGION`, `OBJECT_STORAGE_ACCESS_KEY_ID`, `OBJECT_STORAGE_SECRET_ACCESS_KEY` — storage credentials and region.

Production startup fails closed if PostgreSQL or object storage configuration is missing. Use provider-issued secrets; do not put credentials in GitHub or in frontend variables.

## Database and image storage

PostgreSQL stores accounts, role/state, analyses, notifications, original-image object references, and generated preview references. SQLite remains the local default. The existing migration script at `backend/scripts/migrate_sqlite_to_postgres.py` is retained; it is read-only against the SQLite source, defaults to dry-run, and refuses a non-empty destination. Do not run it until the target PostgreSQL database exists, is confirmed empty, and the migration has been reviewed. The original SQLite databases are not changed by deployment setup.

Original uploads and generated previews use a private S3-compatible bucket, not an ephemeral Vercel filesystem or PostgreSQL BLOBs. The browser obtains a short-lived, user-scoped presigned PUT URL from authenticated FastAPI, uploads directly to object storage, then submits the object key for analysis. The API validates the user's key namespace and image when it retrieves the object. Original-image downloads and generated-preview URLs require an authenticated owner or a short-lived signed preview token. Bucket CORS must allow the exact Vercel frontend origins and `PUT` with the `Content-Type` header. Do not make the bucket public.

The application enforces 15 MiB per image. Direct uploads avoid Vercel Function request-body limits for image bytes, but the analysis request and ordinary API responses still pass through Vercel Functions. Large paired-image processing can also encounter function memory/time limits; test representative images on a preview before production. PDF reports and original-image downloads are streamed.

The migration preserves existing image previews in analysis results. Original uploads from before deployment are only recoverable if they existed in the old database; no absent source image can be reconstructed. Generated PDFs are created from analysis data when requested, rather than stored as separate files.

## Migration procedure

After PostgreSQL is provisioned and verified empty, run a dry-run from a trusted local machine. Keep the database URL out of shell history and source control:

```powershell
cd backend
$env:DATABASE_URL = Read-Host "PostgreSQL URL"
python scripts/migrate_sqlite_to_postgres.py --source satquery.db
```

Review source/target counts and the proposed records. Only after explicit approval, run the script with `--apply`; then verify users, analyses, notifications, timestamps, roles, hashes, and previews in PostgreSQL. The script never modifies or deletes the SQLite source. Existing user password hashes are retained; users may need to sign in again if the production signing key differs.

## Git and deployment

1. Ensure `.gitignore` excludes `.env` files, database files and backups, virtual environments, uploads, build artifacts, and Vercel local state. Keep only placeholder `.env.example` templates.
2. Review the complete staged file list and inspect the repository's existing history before committing. Never force-push over unrelated content or publish database files, credentials, backups, or user data.
3. Connect the intended GitHub repository to Vercel and select the **Services** framework. Verify `app` and `backend` roots and the `/` and `/api/*` paths.
4. Configure the backend variables above in Vercel. Configure the private object bucket's CORS policy for the deployed frontend origin.
5. Deploy a preview first. Check health, login/registration, admin access, all three analyses, history, image retrieval, and PDF generation before production.
6. Configure and verify an empty PostgreSQL database before considering migration. Migration is a separate, approval-gated operation.

Free-service quotas, inactivity behavior, and pricing can change. Check provider terms and current quotas before creating or enabling any service. No provider account or paid plan is created by this repository configuration.

## Tests

From the repository root:

```powershell
npm run build
cd backend
python -m pytest
```

Backend tests use isolated temporary SQLite databases and mock object storage; they do not need production credentials or modify the local application database.
