# Chronos operator dashboard

The worker companion includes a separate Next.js/TypeScript control-plane UI.
It calls only the authenticated FastAPI control API; job execution remains in
the independent allowlisted Python workers. The dashboard is served on loopback
port 3000 in the local Compose profile and the API remains available on port
8000, including its Swagger UI and OpenAPI document.

## Run the complete local product

Docker Desktop is the reproducible local dependency for PostgreSQL, API,
scheduler and three workers. The UI container builds from `web/package-lock.json`
using Node 24 and runs as a non-root, read-only container with bounded CPU,
memory and PID count:

```powershell
docker compose -p chronos -f compose.workers.yaml --profile control --profile workers up --build -d
Invoke-WebRequest http://127.0.0.1:3000
Invoke-WebRequest http://127.0.0.1:8000/docs
```

Open <http://127.0.0.1:3000> and enter the local demonstration token:
`local-development-token-do-not-use-outside-compose-0001`. It is provided only
for disposable local Compose, not an Internet-facing deployment. The token is
kept in the current tab's `sessionStorage` and sent to a same-origin route
handler, which forwards it only to allowlisted Chronos API paths. The API
origin is configured on the server and is not a browser URL. Production
deployment requires a proper secret, TLS, trusted ingress/access policy and
XSS-aware token/session design. The same-origin proxy is not a substitute for
those controls.

Stop and remove the local database after use:

```powershell
docker compose -p chronos -f compose.workers.yaml --profile control --profile workers down --volumes
```

## Frontend development and checks

Node 24 and npm are used by the container. From repository root:

```powershell
npm ci --prefix web
npm --prefix web run dev
npm --prefix web run lint
npm --prefix web run build
```

For local development, start the Compose API and set `CHRONOS_API_URL` to its
reachable origin for the Next server (for example `http://127.0.0.1:8000`), then
open <http://localhost:3000>. Browser journey checks require the complete local
stack and installed Playwright Chromium. `scripts/verify-dashboard.ps1` creates
unique disposable Compose projects, runs real PostgreSQL integration tests,
starts the control plane and three workers, exercises authenticated proxy and
Swagger routes, executes browser journeys, captures screenshots, records logs,
and removes only its own projects and volumes.

## Data and behavior

Overview, job lists/details, create, schedules, worker states, dead letters,
observability and shared queue defaults use the worker API. Charts are built
from PostgreSQL job lifecycle events; latency percentiles measure claim-to-
success elapsed time for completed work. This is an event-derived operational
view, not a dedicated metrics warehouse. Browser reads poll without overlapping
requests and use a 15-second minimum interval. Client-side theme and polling
preferences persist in local storage; API credentials do not.

Job submission uses a stable idempotency key for retries of the same form
submission. Cancellation, retry, replay and schedule disabling require an
explicit confirmation and audit reason. Server authorization, schema checks,
rate limits, request size limits and database uniqueness remain authoritative;
the UI is not an authorization boundary.

## Verified UI captures

Screenshots are captured from the live local app and real PostgreSQL/worker data
by the verification journey. `docs/verification/phase-6-dashboard.txt` records
the exact commands, suite output and lifecycle logs.

- Overview light: `docs/verification/screenshots/phase-6/overview-light.png`
- Overview dark: `docs/verification/screenshots/phase-6/overview-dark.png`
- Overview mobile: `docs/verification/screenshots/phase-6/overview-mobile.png`
- Jobs: `docs/verification/screenshots/phase-6/jobs.png`
- Create job: `docs/verification/screenshots/phase-6/create-job.png`
- Job details: `docs/verification/screenshots/phase-6/job-details.png`
- Schedules: `docs/verification/screenshots/phase-6/schedules.png`
- Workers: `docs/verification/screenshots/phase-6/workers.png`
- Dead letter: `docs/verification/screenshots/phase-6/dead-letter.png`
- Observability: `docs/verification/screenshots/phase-6/observability.png`
- Settings: `docs/verification/screenshots/phase-6/settings.png`
