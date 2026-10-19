# Mr Analytics

Mr Analytics is a data analytics and research workspace. Customers can sign in, upload a CSV, and return to their saved workspaces. Reports include missing values, duplicate rows, numeric statistics, charts, and a preview. Each saved workspace keeps its dataset, report, notes, chart selection, and AI conversation. The research pages help turn a question into an analysis plan. Optional AI questions explain the uploaded report in plain language.

The application no longer includes online banking, account balances, deposits, withdrawals, transfers, or cryptocurrency services. Customer accounts use the existing project's Supabase Auth service; existing users can keep their email and password. The workspace migration adds analytics tables and does not delete or modify existing financial tables or accounts.

## Run locally

Use Python 3.10 or newer. From this project directory in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python app.py
```

Open http://127.0.0.1:5000. Guest CSV analysis works without a database service, an OpenAI account, or a Supabase configuration. Accounts and saved workspaces require the cloud configuration below. Set `SESSION_SECRET` in `.env` to a long random value before sharing the app or running multiple workers. You can generate one locally:

```powershell
py -c "import secrets; print(secrets.token_urlsafe(48))"
```

Save the following as `sample.csv` and upload it from the analytics workspace:

```csv
month,region,revenue,orders
2026-01,North,12500,84
2026-02,North,14100,93
2026-03,South,13200,88
2026-04,South,,91
```

## Set up accounts and saved workspaces

Use the same Supabase project that holds existing customer accounts. Configure the server's `SUPABASE_URL` and `SUPABASE_KEY` values from that project's API settings; `SUPABASE_KEY` must be the project's public anon/publishable key. Keep real configuration in `.env` locally or the hosting provider's environment settings. No service-role key is needed: requests use each signed-in customer's access token, and row-level security restricts workspace access to that customer.

Run [database/workspaces.sql](database/workspaces.sql) in the Supabase SQL editor before deploying this feature. The migration creates only the analytics workspace resources and their ownership policies. It does not copy expired guest reports into accounts. If the project or migration is missing, account features show an unavailable message; the app never labels a local temporary report as saved to an account.

To check the complete customer workflow:

1. Open `/login.html`, create an account, and confirm the email if the Supabase project requires confirmation. Existing customers can sign in with their existing credentials.
2. Open **My dashboard** and upload a CSV. Confirm the upload is marked as saved to your account.
3. Open the saved report, edit its title and notes, and choose a numeric column for its chart. Wait for the save confirmation.
4. Sign out, close the browser, and sign in again. The workspace list, report, notes, and chart selection should return. A second browser signed in to the same account should see the same saved work.
5. Sign in with a different account and verify it cannot see or open the first customer's workspace. Delete a test workspace and verify it no longer appears after signing in again.

Saved workspaces remain in Supabase across browser sessions and application restarts until their owner deletes them. The source CSV is stored with the report in the private analytics tables; no separate Storage bucket is required. Each account can keep up to 25 datasets and 50 MiB in total across CSVs, reports, notes, view state, and AI history. Existing account credentials are retained, but guest reports from the previous version still have their original one-hour lifetime and browser-session ownership. Expired or lost guest reports cannot be recovered; upload their source CSV again while signed in.

An unexpired guest report can be saved to an account after signing in from the same browser. This saves its analysis and future workspace edits; its source CSV is unavailable because guest uploads do not retain raw files. Upload the original CSV while signed in if you also need to download it later.

## Enable optional AI questions

CSV profiling and charts run locally in the server process. AI answers require these additional values in `.env`:

```dotenv
OPENAI_API_KEY=your-server-side-api-key
OPENAI_MODEL=a-model-available-to-your-api-account
ANALYST_ACCESS_TOKEN=your-long-random-private-access-token
```

The app deliberately has no default AI model. Choose a Responses-compatible model your account can access; model availability and pricing depend on your provider account. The integration follows the [OpenAI Responses API](https://developers.openai.com/api/reference/python/resources/responses/methods/create). Keep the API key on the server. The analyst token is a shared access credential for authorized users, so use a separate random value and rotate it if exposed.

The AI assistant displays whether the server has the required configuration. Enter the analyst access token in the assistant and explicitly consent before asking a question. Questions send a bounded summary of the report to OpenAI. Review the consent text before sending sensitive column names or values. AI responses can be incomplete or incorrect; verify claims against the report. Uploading alone does not make a paid AI request.

No API credentials are included, and configuring environment variables does not verify provider access. Without AI configuration, the application continues to provide CSV profiling and research guidance.

## Storage, limits, and access

Uploads are parsed in memory, including multipart handling. When signed in, uploads are saved to the customer's Supabase workspace with the source CSV, analysis report, notes, chart selection, and AI messages. The server verifies account ownership for every read, edit, download, and deletion. Workspace updates include a revision number; an older tab receives a conflict instead of silently overwriting newer work. Reports and CSVs may contain sensitive values; deleting a workspace removes its saved content.

Guest reports use local SQLite at `instance/analysis.sqlite3`, become inaccessible after one hour, and belong only to the browser session that uploaded them. Raw guest CSVs are not retained. Expired rows are purged on the next report-store operation; there is no background deletion job. A maximum of 100 guest reports is retained; older reports may be evicted. Ephemeral hosts such as Render can remove these temporary reports on a restart or deploy. `ANALYSIS_DB_PATH` changes only temporary guest storage and is never used as durable account storage.

CSV uploads must use UTF-8, include named columns, and stay within 10 MB, 100,000 data rows, and 100 columns. The HTTP request limit is 11 MB to allow multipart form metadata. The preview is limited to 100 rows. Malformed rows and unsupported file types are rejected.

Saved workspace IDs are opaque and account ownership is checked on the server and by Supabase row-level security. Authentication tokens are stored in HttpOnly cookies. Account changes and workspace edits require a CSRF token and reject a different `Origin` when one is supplied. Paid AI requests additionally require a matching `Origin`, valid analyst token, and explicit consent. Limits are 10 AI questions per hour per owner and 100 per hour across the application. Each workspace keeps up to 200 AI exchanges. These controls bound access and spending. Use HTTPS when hosting, keep deployment logs and the SQLite file private, and configure provider spending limits before inviting users.

Account workspaces use shared Supabase storage. Temporary guest reports and AI request counters still use local SQLite: multiple worker processes on one host can share that path and the session secret. Multiple hosts require coordinated temporary storage and request limits before scaling.

## API

| Endpoint | Behavior |
| --- | --- |
| `GET /healthz` | Local service health check. |
| `GET /api/auth/me` | Returns the current customer, configuration status, and a CSRF token. |
| `POST /api/auth/signup` | Create a Supabase account; email confirmation may be required. |
| `POST /api/auth/login` | Sign in with an existing email and password. |
| `POST /api/auth/logout` | Sign out and clear authentication cookies. |
| `GET /api/workspaces` | List only the signed-in customer's saved workspaces. |
| `POST /api/workspaces` | Save the current browser's unexpired guest report using its `analysis_id`; requires sign-in and CSRF protection. |
| `GET /api/workspaces/<id>` | Open a saved workspace belonging to the signed-in customer. |
| `PATCH /api/workspaces/<id>` | Save title, notes, and display state with `expected_revision`; stale edits return `409`. |
| `DELETE /api/workspaces/<id>` | Delete the current customer's workspace and its saved content. |
| `GET /api/workspaces/<id>/download` | Download the saved source CSV. |
| `POST /upload` | Multipart `file`; signed-in uploads save a workspace and return `persistent: true`; guest uploads expire. |
| `GET /api/analysis/<analysis_id>` | Fetch a saved account report (`ws_<id>`) or an unexpired report belonging to the current guest session. |
| `DELETE /api/analysis/<analysis_id>` | Delete an owned report. |
| `GET /api/ai-status` | Reports whether optional AI settings are present. |
| `POST /api/analysis/<analysis_id>/question` | JSON `question` and `consent: true`; requires `X-Analyst-Token` or a bearer token and a matching `Origin`. |

`/platform.html` is the customer's private workspace dashboard and redirects signed-out visitors to `/login.html`. `/dashboard.html` remains the CSV analysis page. Removed financial pages and APIs return an unavailable response. Guest analysis remains available without registration.

## Verify and deploy

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Tests cover CSV validation, analytics and research routes, removed financial endpoints, session isolation, report deletion, account access, saved workspace ownership, revision conflicts, and protected AI requests. Provider calls are mocked; they do not spend API credits. Mocked tests do not prove the live Supabase migration or hosting configuration is installed: complete the sign-out/sign-in workflow above against the configured deployment as well.

With Node.js installed, `npm run check` checks all three application scripts and `npm test` runs the Python suite plus analytics and account frontend tests. No npm packages are needed. The frontend tests use a small DOM substitute; they do not verify visual layout in a real browser.

Use Gunicorn on a Linux production host after installing the requirements and setting the environment variables:

```sh
gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 60 app:app
```

Configure a stable `SESSION_SECRET`, HTTPS, `COOKIE_SECURE=true`, and a private writable SQLite path for temporary guest reports. Install the Supabase migration and configure the account environment values before deploying customer workspaces. Render's application filesystem is temporary, so it must not hold the only copy of customer work. Set `PUBLIC_ORIGIN` to the exact public origin, such as `https://mr-analytics-hub.onrender.com`, when your host terminates HTTPS before forwarding requests to Flask. Leave it blank on localhost. Keep `.env`, API keys, the analyst token, and `instance/` out of Git. Local edits do not update an existing live deployment until the updated code is deployed.
