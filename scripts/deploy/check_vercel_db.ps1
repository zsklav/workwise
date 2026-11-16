# Tries the exact database settings Vercel uses (transaction pooler, port 6543) from this computer.
# Run from the project folder:  powershell -ExecutionPolicy Bypass -File scripts\deploy\check_vercel_db.ps1
# The password is typed here only and is never saved or printed.
$env:DB_HOST = "aws-1-ap-southeast-2.pooler.supabase.com"
$env:DB_PORT = "6543"
$env:DB_NAME = "postgres"
$env:DB_USER = "postgres.rkwyvighindanwdphhts"
$env:DB_POOLER = "true"
$env:PGSSLMODE = "require"
$secure = Read-Host "Supabase database password (the same one you put in Vercel)" -AsSecureString
$env:DB_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))

$python = Join-Path $PSScriptRoot "..\..\.venv\Scripts\python.exe"
& $python -c @"
from sqlalchemy import text
from workwise.db.session import engine
try:
    with engine.connect() as c:
        print('CONNECTED. Schema version:', c.scalar(text('SELECT version_num FROM public.alembic_version')))
except Exception as e:
    orig = getattr(e, 'orig', e)
    print('FAILED:', type(orig).__name__, '-', str(orig).strip().splitlines()[0][:300])
"@
