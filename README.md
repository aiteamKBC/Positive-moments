# Positive Mentions

Positive Mentions is an authenticated internal dashboard for browsing V5 lecture
analysis results, reviewing positive learner quotes, and opening SharePoint recordings
at the exact moment a quote occurred.

The browser talks only to Django. Database credentials and SharePoint URL processing
remain on the backend. The existing `public.qa_doctors_sessions` table is represented
by an unmanaged model and is never created or changed by this project.

## Project structure

```text
Positive Mentions/
├── .venv/                  # Existing Python environment (preserved)
├── backend/
│   ├── config/             # Django settings and root URLs
│   ├── positive_mentions/  # API, unmanaged model, services, utilities, tests
│   ├── .env                # Local secrets (ignored by Git)
│   ├── .env.example
│   ├── manage.py
│   └── requirements.txt
├── frontend/
│   ├── src/                # Vue views, components, API client, types
│   ├── .env.example
│   └── package.json
├── .gitignore
└── README.md
```

## Backend setup

From PowerShell in the project root:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\backend\requirements.txt
```

Edit `backend/.env`:

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
DJANGO_SECRET_KEY=replace-with-a-long-random-production-secret
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
CORS_ALLOWED_ORIGINS=http://localhost:5173
```

`DATABASE_URL` is the only place the Neon connection string should be stored. The
unqualified unmanaged table name uses PostgreSQL's default `public` schema. This is
compatible with Neon's pooled connection endpoints, which reject `search_path` as a
startup parameter. Never commit `.env`.

When no `DATABASE_URL` is present, Django uses local SQLite for auth/setup work only;
lecture data will not exist there. Real lecture data always comes from Neon.

Apply Django's own authentication, admin, session, and token migrations:

```powershell
cd .\backend
python manage.py migrate
```

These migrations do not create or modify `qa_doctors_sessions`; that model has
`managed = False`.

### Create the first admin/staff user

With the backend environment activated:

```powershell
cd .\backend
python manage.py createsuperuser
```

Follow the prompts. The same credentials work on the frontend login page and at
`/admin/`.

### Run the backend

```powershell
cd .\backend
..\.venv\Scripts\python.exe manage.py runserver
```

The API is then available at `http://127.0.0.1:8000/`.

## V5 data scope

The dashboard base scope includes every row where
`clips_analysis_completeness = 'positive_clips_v5_final'`. Optional filters selected
on the lectures page are applied to both the database summary and the paginated list.

## Frontend setup

In another PowerShell window:

```powershell
cd .\frontend
npm install
npm run dev
```

Open `http://localhost:5173/`. Vite proxies `/api` to the local Django server. For a
separately hosted API, copy `frontend/.env.example` to `.env.local` and set:

```dotenv
VITE_API_BASE_URL=https://your-api-host.example/api
```

## Authentication

The initial implementation uses Django users and REST Framework tokens:

- Login returns a token stored by the frontend.
- Every Positive Mentions endpoint requires that token.
- Logout revokes the active token.
- Database credentials are never sent to the browser.

Authentication calls are isolated in `frontend/src/services/api.ts` and the backend
auth URLs/views so Microsoft Entra ID can replace this layer later.

## API endpoints

All endpoints below require `Authorization: Token <token>`, except login.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/auth/login/` | Authenticate with username/password |
| POST | `/api/auth/logout/` | Revoke the current token |
| GET | `/api/auth/me/` | Return the current user |
| GET | `/api/positive-mentions/summary/` | Database-calculated dashboard totals |
| GET | `/api/positive-mentions/lectures/` | Filtered, paginated lecture list |
| GET | `/api/positive-mentions/lectures/<session-key>/` | Lecture and normalized clips |
| GET | `/api/positive-mentions/lectures/<session-key>/clips/<index>/watch/` | Timestamped SharePoint URL |

Lecture list query parameters:

`page`, `page_size`, `search`, `trainer`, `date_from`, `date_to`, `clips_status`,
`has_positive_clips` (`true`/`false`), and `recording_status`
(`available`/`missing`). `clip_production_status` accepts `all`, `ready`, `pending`,
or `no_positive_moments`. Ready clips come from `qa_positive_clip_assets` rows with
`trim_status = 'completed'` and a permanent HTTPS SharePoint organization `clip_url`;
the lecture recording URL is not used for clip-production status. Lecture detail
responses attach each asset using its one-based original `clip_index`, validated by
the workflow's `session_id:start_cue:end_cue` `clip_key` and exact
`source_start`/`source_end`. The existing timestamped full-lecture watch endpoint and
the trimmed clip URL remain separate actions.

Because raw session IDs can contain URL-special characters, list responses include a
URL-safe `session_key`. Detail and watch routes use that key while still returning the
original `session_id` in response data.

The watch endpoint reads both the recording URL and clip timestamp from Neon. It does
not accept either value from the browser.

## Checks and tests

Backend:

```powershell
cd .\backend
..\.venv\Scripts\python.exe manage.py check
..\.venv\Scripts\python.exe manage.py test
```

Frontend:

```powershell
cd .\frontend
npm run lint
npm run type-check
npm run build
```

The production frontend output is written to `frontend/dist/`.
