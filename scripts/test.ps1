# One command per test category, plus the quality gate and the full suite.
#   powershell -ExecutionPolicy Bypass -File scripts/test.ps1 -Category unit
# Categories: unit | integration | e2e | security | concurrency | failure | frontend | all | coverage | quality
# Everything except `unit` needs the native services (scripts/dev_services.ps1 start).
# The suite never calls a paid or hosted model (LLM_* is blanked by backend/tests/conftest.py).
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('unit', 'integration', 'e2e', 'security', 'concurrency', 'failure', 'frontend', 'all', 'coverage', 'quality')]
  [string]$Category,
  [string[]]$Extra = @()
)
$ErrorActionPreference = 'Stop'
if (-not $env:UV_PROJECT_ENVIRONMENT) { $env:UV_PROJECT_ENVIRONMENT = "$env:USERPROFILE/devtools/venv" }

function Run($arguments) {
  Write-Host ">> uv run --no-sync $($arguments -join ' ')" -ForegroundColor Cyan
  & uv run --no-sync @arguments
  if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $($arguments -join ' ')" -ForegroundColor Red; exit $LASTEXITCODE }
}

switch ($Category) {
  'unit'        { Run (@('pytest', 'backend/tests', '-m', 'unit', '-q') + $Extra) }
  'integration' { Run (@('pytest', 'backend/tests', '-m', 'integration and not docling and not real_model', '-q') + $Extra) }
  'e2e'         { Run (@('pytest', 'backend/tests', '-m', 'e2e', '-q') + $Extra) }
  'security'    { Run (@('pytest', 'backend/tests', '-m', 'security', '-q') + $Extra) }
  'concurrency' { Run (@('pytest', 'backend/tests', '-m', 'concurrency', '-q') + $Extra) }
  'failure'     { Run (@('pytest', 'backend/tests', '-m', 'failure', '-q') + $Extra) }
  'frontend'    { Run (@('pytest', 'backend/tests', '-k', 'frontend', '-q') + $Extra) }
  'all'         { Run (@('pytest', 'backend/tests', '-q') + $Extra) }
  'coverage'    {
    # the full suite with line coverage of the in-process code (sandboxed child processes and worker
    # processes are not measured). COVERAGE_CORE=sysmon is the fast, Python 3.12+ tracer.
    $env:COVERAGE_CORE = 'sysmon'
    New-Item -ItemType Directory -Force -Path 'data' | Out-Null
    Run (@('pytest', 'backend/tests', '-q', '--cov=backend/app', '--cov=frontend', '--cov-report=term', '--cov-report=json:data/coverage.json') + $Extra)
    Write-Host 'JSON report: data/coverage.json' -ForegroundColor Yellow
  }
  'quality' {
    Run @('ruff', 'format', '--check', '.')
    Run @('ruff', 'check', '.')
    Run @('pyright')
    # migrations: upgrade -> downgrade -> upgrade on the throw-away test database
    Run @('pytest', 'backend/tests/integration/test_database_migrations.py', '-q')
    Write-Host 'Docker smoke test: scripts/verify_g1.ps1 (needs Docker; see KI-014).' -ForegroundColor Yellow
  }
}
Write-Host "OK: $Category" -ForegroundColor Green
