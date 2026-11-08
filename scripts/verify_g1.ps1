# Gate G1 verification (needs Docker Desktop RUNNING). Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File scripts/verify_g1.ps1
# It builds the images, starts the full stack (postgres, redis, qdrant, migrate, api, worker, beat)
# from clean volumes, checks health, runs migrations, proves the Linux sandbox limits, ingests a real
# PDF end to end, restarts the containers and proves data persisted. It exits non-zero on the first
# failed check. Paste the output into docs/EXECUTION_LOG.md.
$ErrorActionPreference = 'Stop'
function Step($m) { Write-Host "`n=== $m" -ForegroundColor Cyan }
function Check($cond, $m) { if (-not $cond) { Write-Host "FAIL: $m" -ForegroundColor Red; exit 1 } else { Write-Host "ok: $m" } }
function WaitReady($minutes) {
  $deadline = (Get-Date).AddMinutes($minutes)
  do { Start-Sleep 5; $r = try { (Invoke-WebRequest http://127.0.0.1:8000/api/v1/health/ready -UseBasicParsing -TimeoutSec 5).StatusCode } catch { 0 } } until ($r -eq 200 -or (Get-Date) -gt $deadline)
  return $r
}
function Api($method, $path, $body, $token) {
  $h = @{}; if ($token) { $h['Authorization'] = "Bearer $token" }
  Invoke-RestMethod "http://127.0.0.1:8000/api/v1$path" -Method $method -Headers $h -ContentType 'application/json' -Body $(if ($body) { $body | ConvertTo-Json } else { $null })
}

Step 'docker available'
docker version --format '{{.Server.Version}}'; Check ($LASTEXITCODE -eq 0) 'docker daemon reachable'
docker compose version; Check ($LASTEXITCODE -eq 0) 'docker compose available'
if (-not (Test-Path .env)) { Write-Host 'Create .env from .env.example first (COMPOSE_POSTGRES_PASSWORD, JWT_SECRET, MFA_ENCRYPTION_KEY).'; exit 1 }

Step 'clean slate + build'
docker compose down -v --remove-orphans
docker compose build; Check ($LASTEXITCODE -eq 0) 'images build (CPU-only torch, Docling, RapidOCR)'

Step 'start the stack'
docker compose up -d; Check ($LASTEXITCODE -eq 0) 'compose up'
Check ((WaitReady 5) -eq 200) '/health/ready returns 200 (PostgreSQL, Redis, Qdrant, storage)'
docker compose ps
$w = try { (Invoke-WebRequest http://127.0.0.1:8000/api/v1/health/workers -UseBasicParsing -TimeoutSec 15).StatusCode } catch { 0 }
Check ($w -eq 200) '/health/workers returns 200 (celery worker answers ping)'
Check ((docker compose ps --status running --services | Select-String -Pattern '^beat$').Count -eq 1) 'celery beat is running (outbox dispatcher + reconciler + orphan sweep)'

Step 'migrations'
docker compose run --rm migrate alembic -c alembic.ini current; Check ($LASTEXITCODE -eq 0) 'alembic current'

Step 'Linux sandbox limits (timeout kill, memory cap, no secrets, warm recycling)'
docker compose run --rm worker python -m app.ingestion.sandbox_selfcheck; Check ($LASTEXITCODE -eq 0) 'sandbox self-check passes on Linux'

Step 'ingest a real PDF end to end through the containers'
$email = "g1-$([guid]::NewGuid().ToString('N').Substring(0,8))@example.com"
$pw = 'correct-horse-battery-staple'
Api 'POST' '/auth/register' @{ email = $email; password = $pw } $null | Out-Null
$token = (Api 'POST' '/auth/login' @{ email = $email; password = $pw } $null).access_token
Check ([bool]$token) 'registered and logged in'
docker compose exec -T api python -c "from tests.pdfs import sample_text_pdf; open('/tmp/g1.pdf','wb').write(sample_text_pdf('g1'))"
docker compose cp api:/tmp/g1.pdf ./g1.pdf
$up = curl.exe -s -X POST http://127.0.0.1:8000/api/v1/documents/upload -H "Authorization: Bearer $token" -F "file=@g1.pdf;type=application/pdf" | ConvertFrom-Json
Check ($up.document.status -eq 'queued') 'upload accepted (202, queued)'
$docId = $up.document.id
$deadline = (Get-Date).AddMinutes(10)
do { Start-Sleep 5; $doc = Api 'GET' "/documents/$docId" $null $token } until ($doc.status -in 'completed','failed' -or (Get-Date) -gt $deadline)
Check ($doc.status -eq 'completed') "document completed (parser=$($doc.parser), chunks=$($doc.chunk_count))"
Remove-Item g1.pdf -ErrorAction SilentlyContinue

Step 'restart everything, then recreate containers: data must persist'
docker compose restart
Check ((WaitReady 5) -eq 200) 'stack healthy again after restart'
$doc = Api 'GET' "/documents/$docId" $null $token
Check ($doc.status -eq 'completed') 'document still completed after restart (PostgreSQL + Qdrant + object storage persisted)'
docker compose down
docker compose up -d
Check ((WaitReady 5) -eq 200) 'healthy after containers were recreated'
$login = Api 'POST' '/auth/login' @{ email = $email; password = $pw } $null
Check ([bool]$login.access_token) 'user registered before recreation can still log in'
$doc = Api 'GET' "/documents/$docId" $null $login.access_token
Check ($doc.status -eq 'completed' -and $doc.chunk_count -gt 0) 'document and vectors survived container recreation (named volumes)'

Write-Host "`nG1 CHECKS PASSED" -ForegroundColor Green
