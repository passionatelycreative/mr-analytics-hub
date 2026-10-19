# Mr Analytics

Mr Analytics is a data analytics and research workspace. Upload a CSV to inspect its structure, missing values, duplicate rows, numeric statistics, charts, and a preview. The research pages help turn a question into an analysis plan. Optional AI questions explain the uploaded report in plain language.

The application no longer includes online banking, account balances, deposits, withdrawals, transfers, or cryptocurrency services. Existing Supabase data is not read, migrated, or deleted by this version.

## Run locally

Use Python 3.10 or newer. From this project directory in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python app.py
```

Open http://127.0.0.1:5000. CSV analysis works without a database service, an OpenAI account, or a Supabase configuration. Set `SESSION_SECRET` in `.env` to a long random value before sharing the app or running multiple workers. You can generate one locally:

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

Uploads are parsed in memory, including multipart handling. Raw CSV files are not retained. Analysis reports are stored temporarily in local SQLite at `instance/analysis.sqlite3`, become inaccessible after one hour, and are tied to the browser session that uploaded them. Expired rows are purged on the next report-store operation; there is no background deletion job. Reports include a data preview and may contain sensitive values. Use the report deletion action to remove a report sooner. A maximum of 100 reports is retained; older reports may be evicted. Restarting on ephemeral hosting can also remove reports. The optional `ANALYSIS_DB_PATH` changes the SQLite file location.

CSV uploads must use UTF-8, include named columns, and stay within 10 MB, 100,000 data rows, and 100 columns. The HTTP request limit is 11 MB to allow multipart form metadata. The preview is limited to 100 rows. Malformed rows and unsupported file types are rejected.

Report IDs are opaque and session ownership is checked for reading, deleting, and questioning reports. Paid AI requests require a valid analyst token, explicit consent, and a matching `Origin` header. Limits are 10 AI questions per hour per browser session and 100 per hour across the application. These controls bound access and spending; the shared token is not a complete user account system. Use HTTPS when hosting, keep deployment logs and the SQLite file private, and configure provider spending limits before inviting users.

This is a single-instance application. Multiple worker processes on one host can share the SQLite path and session secret. Multiple hosts need shared storage and coordinated configuration before scaling.

## API

| Endpoint | Behavior |
| --- | --- |
| `GET /healthz` | Local service health check. |
| `POST /upload` | Multipart `file` field; returns `analysis` and a session-owned `analysis_id`. |
| `GET /api/analysis/<analysis_id>` | Fetch an unexpired report belonging to the current session. |
| `DELETE /api/analysis/<analysis_id>` | Delete the current session's report. |
| `GET /api/ai-status` | Reports whether optional AI settings are present. |
| `POST /api/analysis/<analysis_id>/question` | JSON `question` and `consent: true`; requires `X-Analyst-Token` or a bearer token and a matching `Origin`. |

Removed financial pages and APIs return an unavailable response. `/login.html` redirects to the analytics platform; this version does not require banking registration to analyze a CSV.

## Verify and deploy

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Tests cover CSV validation, analytics and research routes, removed financial endpoints, session isolation, report deletion, and protected AI requests. Provider calls are mocked; they do not spend API credits.

With Node.js installed, `npm run check` checks the workspace JavaScript and `npm test` runs both the Python suite and the six frontend interaction tests. No npm packages are needed. The frontend tests use a small DOM substitute; they do not verify visual layout in a real browser.

Use Gunicorn on a Linux production host after installing the requirements and setting the environment variables:

```sh
gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 60 app:app
```

Configure a stable `SESSION_SECRET`, HTTPS, `COOKIE_SECURE=true`, a private writable SQLite path, and an appropriate hosting disk. Set `PUBLIC_ORIGIN` to the exact public origin, such as `https://analytics.example.com`, when your host terminates HTTPS before forwarding requests to Flask. Leave it blank on localhost. Keep `.env`, API keys, the analyst token, and `instance/` out of Git. Local edits do not update an existing live deployment until the updated code is deployed.
