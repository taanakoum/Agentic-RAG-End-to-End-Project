# Native Windows dev services (PostgreSQL, Redis-compatible, Qdrant). DEV ONLY.
# Docker is still required for gates G1/G11/G12 (see docs/KNOWN_ISSUES.md KI-003).
# Usage: powershell -File scripts/dev_services.ps1 start|stop|status
param([Parameter(Mandatory=$true)][ValidateSet('start','stop','status')][string]$Action)
$ErrorActionPreference = 'Stop'
$Root   = if ($env:DEVTOOLS_DIR) { $env:DEVTOOLS_DIR } else { Join-Path $env:USERPROFILE 'devtools' }
$PgBin  = Join-Path $Root 'postgres\pgsql\bin'
$PgData = Join-Path $Root 'data\pg'
$Redis  = Get-ChildItem (Join-Path $Root 'redis') -Recurse -Filter redis-server.exe | Select-Object -First 1
$Qdrant = Join-Path $Root 'qdrant\qdrant.exe'
$Logs   = Join-Path $Root 'logs'

function Test-Port($p) { [bool](Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) }

function Show-Status {
  foreach ($s in @(@('postgres',5432),@('redis',6379),@('qdrant-http',6333),@('qdrant-grpc',6334))) {
    '{0,-12} port {1}: {2}' -f $s[0], $s[1], $(if (Test-Port $s[1]) {'UP'} else {'down'})
  }
}

switch ($Action) {
  'start' {
    if (-not (Test-Port 5432)) { & "$PgBin\pg_ctl.exe" -D $PgData -l "$Logs\postgres.log" -w start | Out-Null }
    if (-not (Test-Port 6379)) {
      Start-Process -WindowStyle Hidden -FilePath $Redis.FullName -WorkingDirectory (Join-Path $Root 'data\redis') `
        -ArgumentList '--bind','127.0.0.1','--port','6379','--appendonly','yes','--dir',(Join-Path $Root 'data\redis') `
        -RedirectStandardOutput "$Logs\redis.log" -RedirectStandardError "$Logs\redis.err.log"
    }
    if (-not (Test-Port 6333)) {
      $env:QDRANT__SERVICE__HOST = '127.0.0.1'
      $env:QDRANT__STORAGE__STORAGE_PATH = (Join-Path $Root 'data\qdrant\storage')
      $env:QDRANT__STORAGE__SNAPSHOTS_PATH = (Join-Path $Root 'data\qdrant\snapshots')
      $env:QDRANT__TELEMETRY_DISABLED = 'true'
      Start-Process -WindowStyle Hidden -FilePath $Qdrant -WorkingDirectory (Join-Path $Root 'data\qdrant') `
        -RedirectStandardOutput "$Logs\qdrant.log" -RedirectStandardError "$Logs\qdrant.err.log"
    }
    Start-Sleep -Seconds 4; Show-Status
  }
  'stop' {
    if (Test-Port 5432) { & "$PgBin\pg_ctl.exe" -D $PgData -m fast -w stop | Out-Null }
    Get-Process -Name redis-server,qdrant -ErrorAction SilentlyContinue | Stop-Process
    Start-Sleep -Seconds 1; Show-Status
  }
  'status' { Show-Status }
}
