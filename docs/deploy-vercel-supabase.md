# Deploy on Vercel with Supabase (free, demo)

Vercel runs the FastAPI app (`main.py` exposes `app`). Supabase holds the PostgreSQL
database. This is a reduced demo: RabbitMQ, the notification service, monitoring, resume
upload and password reset email are not part of it.

## 1. Collect the Supabase connection details

In the Supabase project open **Connect**. Note three things, and keep the password private:

| Use | Host | Port |
|---|---|---|
| Migrations (run once from your computer) | Session pooler | 5432 |
| The API on Vercel | Transaction pooler | 6543 |

The user name looks like `postgres.<project-ref>`. The database name is `postgres`.
The direct host can be IPv6 only on the free plan, so use the pooler hosts.

## 2. Run the migrations from your computer

Set these in your own terminal (never commit them). Use the **session pooler** host and port 5432.

```powershell
$env:DB_HOST="<session pooler host>"; $env:DB_PORT="5432"; $env:DB_NAME="postgres"
$env:DB_USER="postgres.<project-ref>"; $env:DB_PASSWORD="<database password>"
$env:MIGRATION_DB_USER=$env:DB_USER; $env:MIGRATION_DB_PASSWORD=$env:DB_PASSWORD
$env:JWT_SECRET="<any 32+ character random text>"; $env:PGSSLMODE="require"
python -m scripts.database.migrate
```

Then create the first admin the same way (it asks for a password):

```powershell
python -m workwise.cli.manage_users create-admin <username>
```

## 3. Create the Vercel project

1. Sign in to Vercel with GitHub, choose **Add New > Project**, and import `zsklav/workwise`.
2. Leave the framework on **FastAPI** or automatic. No build command is needed.
3. Add these environment variables (the transaction pooler values):

| Name | Value |
|---|---|
| `DB_HOST` | transaction pooler host |
| `DB_PORT` | `6543` |
| `DB_NAME` | `postgres` |
| `DB_USER` | `postgres.<project-ref>` |
| `DB_PASSWORD` | database password |
| `DB_POOLER` | `true` |
| `PGSSLMODE` | `require` |
| `JWT_SECRET` | a different random text of 32+ characters |
| `RESUME_ENABLED` | `false` |
| `API_IP_LIMIT` | `3000` |
| `CORS_ORIGINS` | your front end origin, or leave unset |

4. Deploy. Then open `https://<project>.vercel.app/ready` and `/docs`.

## Limits of this setup

- Free Supabase projects pause after one week without use and have no backups
  ([pricing](https://supabase.com/pricing)). Un-pause from the dashboard.
- All visitors share one rate-limit budget, because the app cannot tell Vercel's
  proxy from users. `API_IP_LIMIT=3000` makes that budget roomy.
- Resume upload answers 503. Notifications answer 503. Password reset answers 503.
- Deployed and checked on 2026-11-16: `/ready` returned `{"status":"ready"}`.

## Troubleshooting

- `/ready` says `Database not ready`: open Vercel **Logs** and search `readiness_database_error`.
  The line names the cause. A pasted value can carry a hidden tab or space, which shows as
  `'\taws-...'`; delete the value, type it again, then **Redeploy**.
- To test the Vercel settings from your own computer, run
  `powershell -ExecutionPolicy Bypass -File scripts\deploy\check_vercel_db.ps1`.
- One-time database setup (migrations and first admin) is
  `scripts\deploy\supabase_setup.ps1`.
