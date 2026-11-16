# One-time Supabase setup: applies migrations, then creates the first admin.
# Run from the project folder:  powershell -ExecutionPolicy Bypass -File scripts\deploy\supabase_setup.ps1
# The password is typed into this window only. It is never saved to a file.
$ErrorActionPreference = "Stop"

$env:DB_HOST = "aws-1-ap-southeast-2.pooler.supabase.com"
$env:DB_PORT = "5432"
$env:DB_NAME = "postgres"
$env:DB_USER = "postgres.rkwyvighindanwdphhts"
$secure = Read-Host "Supabase database password" -AsSecureString
$env:DB_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$env:MIGRATION_DB_USER = $env:DB_USER
$env:MIGRATION_DB_PASSWORD = $env:DB_PASSWORD
$env:JWT_SECRET = "temporary-local-migration-secret-0123456789abcdef"
$env:PGSSLMODE = "require"

$python = Join-Path $PSScriptRoot "..\..\.venv\Scripts\python.exe"

Write-Host "`n[1/2] Applying migrations..."
& $python -m scripts.database.migrate
if ($LASTEXITCODE -ne 0) { throw "Migration failed. Copy the last lines above (without any password) and send them." }

Write-Host "`n[2/2] Creating the first admin."
$username = Read-Host "Admin username (small letters, digits, underscore)"
& $python -m workwise.cli.manage_users create-admin $username
if ($LASTEXITCODE -ne 0) { throw "Admin creation failed. Send the message above." }

Write-Host "`nDone. Database is ready."
