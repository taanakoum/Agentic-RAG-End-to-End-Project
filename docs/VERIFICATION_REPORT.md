# Hosting preparation verification

Date: 2026-11-14. All changes are uncommitted; no deployment was attempted. Phase 11 is skipped.

## Environment and commands

Native Windows, Python 3.13.12, with existing local PostgreSQL, Redis and Qdrant. The installed
project environment is `C:/Users/ali_a/devtools/venv`. Commands below use its Python executable;
ordinary `uv run --no-sync` could not initialize its external cache under these sandbox permissions.
Pyright must receive `--pythonpath` for that interpreter or it resolves the wrong Python environment.

```powershell
$py = 'C:/Users/ali_a/devtools/venv/Scripts/python.exe'
& $py -m ruff check .
& $py -m ruff format --check .
& $py -m pyright --pythonpath $py
& $py -m pytest -q -x --basetemp=data/pytest-deployment-full -o cache_dir=data/pytest-cache
```

Use a fresh basetemp directory on subsequent runs to avoid reusing artifacts. Test fixtures select
`rag_test`, Redis DB 15, test-prefixed Qdrant collections and temporary object storage. They clear
hosted-model settings so no paid or live hosted LLM is used. PostgreSQL development data is not used.

## Results

| Check | Actual result |
|---|---|
| Targeted configuration/hosted-schema/HTTP/migration/checkpointer/account lifecycle tests | 65 passed in 38.86 s; initial private-schema scenario before purge coverage was extended |
| Sandbox and warm-sandbox suites after timeout fix | 33 passed in 25.67 s |
| Final private-schema/checkpointer/account/DB-TLS regression | 33 passed in 21.85 s; includes real graph persistence, checkpoint deletion and orphan cleanup under a fresh private schema |
| Chat API after test-session cleanup fix | 27 passed in 34.45 s with SQLAlchemy SAWarning treated as errors; no warnings |
| Ruff lint/format | Passed; final check recorded in execution log |
| Strict Pyright | 0 errors, 0 warnings, 0 informations with explicit --pythonpath |
| Full native pytest suite | 1202 passed, 1 skipped, 160 warnings in 1264.13 s (21 min 4 s) |
| Render Blueprint | Validated locally against `https://render.com/schema/render.yaml.json` downloaded on 2026-11-14 |
| Git packaging | No Python source under backend/frontend/scripts or bundled password corpus is ignored by Git after the fix |
| Fresh source snapshot | 261 candidate code/data/asset files copied without .env; fresh subprocess imported API, S3 storage and frontend client and loaded password corpus successfully |
| Docker | `docker --version` failed: executable not available. Build and Compose verification not run |
| Hosted end-to-end | Not run: no cloud resources or credentials provisioned; no deployment URL |

Observed failures were retained and fixed: a baseline unit run had temporary-directory permission
errors; an explicit workspace basetemp resolves them. A second run stopped with **429 passed,
1 failed**: the sandbox timeout lasted 120 seconds; the process-handle fix then passed all sandbox
tests. Initial Pyright invocation selected an incorrect environment; explicit `--pythonpath` fixes
dependency resolution. A later type error in the new test was fixed by narrowing the parsed CA-path
value to a string. YAML's unquoted `off` was parsed as a boolean; quoting it fixed schema validation.

The full run started before the final checkpoint-purge adjustment and private-schema test extension;
the final 33-test regression above exercises that final code in a fresh process. The full run also
found a test-only unclosed session in `test_chat_api.py`; using an async context manager fixed it,
and the fresh 27-test chat rerun above passed with SQLAlchemy warnings promoted to errors.

Default pytest cache writes were denied; the final command moves the cache into ignored workspace
data. The full run's 160 warnings comprised 157 Celery Redis `setex` deprecations, two compatibility
warnings while deliberately taking Qdrant down, and that one now-fixed test-session cleanup warning.
No warnings were hidden. The optional real hosted-model test accounts for the one skip.

Final regression commands:

```powershell
& $py -m pytest backend/tests/integration/test_hosted_schema.py backend/tests/integration/test_checkpointer.py backend/tests/integration/test_account_lifecycle.py backend/tests/unit/test_hosted_database.py -q -x --basetemp=data/pytest-deployment-final-regression -o cache_dir=data/pytest-cache
& $py -m pytest backend/tests/integration/test_chat_api.py -q -x --basetemp=data/pytest-chat-session-cleanup -o cache_dir=data/pytest-cache -W error::sqlalchemy.exc.SAWarning
```

## Interpretation

Local verification demonstrates the implementation against native services and the schema/private
namespace wiring, not production capacity or live Supabase compatibility. No load test was run.
See `HOSTING.md` for the remaining Linux-container, cloud persistence, SMTP, S3 multipart and UI
checks required before calling a hosted deployment verified.
