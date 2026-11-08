# Gate G1 verification (needs Docker Desktop running). Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File scripts/verify_g1.ps1
# It builds the images, starts the stack from clean volumes, checks health, runs migrations,
# writes data, restarts the containers and proves the data persisted. Paste the output into
# docs/EXECUTION_LOG.md. It exits non-zero on the first failed check.
$ErrorActionPreference = 'Stop'
function Step($m) { Write-Host "`n=== $m" -ForegroundColor Cyan }
function Check($cond, $m) { if (-not $cond) { Write-Host "FAIL: $m" -ForegroundColor Red; exit 1 } else { Write-Host "ok: $m" } }

Step 'docker available'
docker version --format '{{.Server.Version}}'; Check ($LASTEXITCODE -eq 0) 'docker daemon reachable'
docker compose version; Check ($LASTEXITCODE -eq 0) 'docker compose available'

if (-not (Test-Path .env)) { Write-Host 'Create .env from .env.example first (COMPOSE_POSTGRES_PASSWORD, JWT_SECRET, MFA_ENCRYPTION_KEY).'; exit 1 }

Step 'clean slate + build'
docker compose down -v --remove-orphans
docker compose build; Check ($LASTEXITCODE -eq 0) 'images build'

Step 'start the stack'
docker compose up -d; Check ($LASTEXITCODE -eq 0) 'compose up'
$deadline = (Get-Date).AddMinutes(4)
do { Start-Sleep 5; $ready = try { (Invoke-WebRequest http://127.0.0.1:8000/api/v1/health/ready -UseBasicParsing -TimeoutSec 5).StatusCode } catch { 0 } } until ($ready -eq 200 -or (Get-Date) -gt $deadline)
Check ($ready -eq 200) '/health/ready returns 200 (PostgreSQL, Redis, Qdrant, storage)'
docker compose ps

Step 'migrations'
docker compose run --rm migrate alembic -c alembic.ini current; Check ($LASTEXITCODE -eq 0) 'alembic current'

Step 'workers'
$w = try { (Invoke-WebRequest http://127.0.0.1:8000/api/v1/health/workers -UseBasicParsing -TimeoutSec 15).StatusCode } catch { 0 }
Check ($w -eq 200) '/health/workers returns 200 (celery worker answers ping)'

Step 'write data, restart everything, read it back'
$email = "g1-$([guid]::NewGuid().ToString('N').Substring(0,8))@example.com"
$body = @{ email = $email; password = 'correct-horse-battery-staple' } | ConvertTo-Json
$r = Invoke-WebRequest http://127.0.0.1:8000/api/v1/auth/register -Method Post -Body $body -ContentType 'application/json' -UseBasicParsing
Check ($r.StatusCode -eq 201) 'registered a user'
docker compose exec -T qdrant bash -c 'true'  # service reachable
docker compose restart
$deadline = (Get-Date).AddMinutes(3)
do { Start-Sleep 5; $ready = try { (Invoke-WebRequest http://127.0.0.1:8000/api/v1/health/ready -UseBasicParsing -TimeoutSec 5).StatusCode } catch { 0 } } until ($ready -eq 200 -or (Get-Date) -gt $deadline)
Check ($ready -eq 200) 'stack healthy again after restart'
$login = try { (Invoke-WebRequest http://127.0.0.1:8000/api/v1/auth/login -Method Post -Body $body -ContentType 'application/json' -UseBasicParsing).StatusCode } catch { 0 }
Check ($login -eq 200) 'user registered before the restart can still log in (PostgreSQL data persisted)'

Step 'full down/up (containers removed, volumes kept)'
docker compose down
docker compose up -d
$deadline = (Get-Date).AddMinutes(3)
do { Start-Sleep 5; $ready = try { (Invoke-WebRequest http://127.0.0.1:8000/api/v1/health/ready -UseBasicParsing -TimeoutSec 5).StatusCode } catch { 0 } } until ($ready -eq 200 -or (Get-Date) -gt $deadline)
Check ($ready -eq 200) 'healthy after containers were recreated'
$login = try { (Invoke-WebRequest http://127.0.0.1:8000/api/v1/auth/login -Method Post -Body $body -ContentType 'application/json' -UseBasicParsing).StatusCode } catch { 0 }
Check ($login -eq 200) 'data persisted across container recreation (named volumes)'

Write-Host "`nG1 CHECKS PASSED" -ForegroundColor Green
