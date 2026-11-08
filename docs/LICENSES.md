# Licences and cost audit

Requirement: everything the project uses must be open source or free. Audited 2026-11-08 from the
pinned dependency set (`uv.lock`) and the runtime services. This is an engineering audit, not legal
advice; re-check before commercial redistribution.

## Python dependencies
All direct dependencies are permissive (MIT / BSD / Apache-2.0 / MPL-2.0-style) except:

| Package | Licence | Note |
|---|---|---|
| psycopg (sync engine for Celery tasks) | LGPL-3.0 | Weak copyleft; used unmodified as a library, which LGPL permits. `asyncpg` (Apache-2.0) is the async driver. |
| certifi, other MPL-2.0 packages | MPL-2.0 | File-level copyleft; unmodified. |

Parsing and ML stack (all free, all run locally, no API keys, no network at query time):
Docling (MIT), RapidOCR (Apache-2.0, PP-OCR ONNX models bundled), pypdf (BSD), pypdfium2
(Apache-2.0/BSD), FastEmbed (Apache-2.0) with BAAI/bge-small-en-v1.5 (MIT) and Qdrant/bm25
(Apache-2.0), onnxruntime (MIT), PyTorch CPU (BSD-3; needed by Docling's layout model).
Rerankers (all run locally via FastEmbed/ONNX): BAAI/bge-reranker-v2-m3 (Apache-2.0, default); for evaluation only Xenova/ms-marco-MiniLM-L-6/L-12 (Apache-2.0) and jinaai/jina-reranker-v1-turbo-en (Apache-2.0). jina-reranker-v2-base-multilingual is CC-BY-NC-4.0 (non-commercial) and was deliberately NOT used.
Docling's layout/table models are downloaded from Hugging Face on first use (free, cached under
`MODEL_CACHE_DIR`).

## Data
- Breached-password list: union of two SecLists top-100k password lists (MIT), committed as
  `backend/app/core/data/common_passwords.txt`. Offline, no k-anonymity API call.
- Evaluation corpus (`evaluation/chunking/corpus.py`): synthetic, authored for this project.

## Services
| Service | Licence | Cost | Notes |
|---|---|---|---|
| PostgreSQL 17 | PostgreSQL licence | free | |
| Qdrant 1.20 | Apache-2.0 | free | self-hosted |
| Redis 8 | AGPLv3 / RSALv2 / SSPL (tri-licence) | free to run | AGPLv3 is OSI-approved. If a permissive licence is required use **Valkey** (BSD-3, protocol compatible): change the image in `docker-compose.yml`. Nothing in the code uses Redis modules. |
| Celery, kombu | BSD-3 | free | |
| Docker Engine / CLI / Compose | Apache-2.0 | free | |
| Docker Desktop | proprietary, free tier | free for personal use, education, non-commercial open source, and companies with < 250 employees AND < USD 10M annual revenue; paid otherwise | Not required: Docker Engine on Linux, Podman, Rancher Desktop or Colima run the same compose file. |
| moto (S3 emulator, tests only) | Apache-2.0 | free | |

## Not used (by design)
No paid API is called by the platform's ingestion path. LLM providers arrive in a later phase and
will be configurable; tests never call a paid LLM (non-negotiable in CLAUDE.md).
