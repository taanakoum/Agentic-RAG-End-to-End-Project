# Documentation

## Start here

| Document | What it answers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the pieces fit and how one question flows through the agent |
| [SECURITY.md](SECURITY.md) | Authentication, isolation between users, upload sandboxing, prompt injection |
| [EVALUATION.md](EVALUATION.md) | Retrieval and answer quality, measured on synthetic and real documents |
| [TESTING_STRATEGY.md](TESTING_STRATEGY.md) | What the 1,200+ tests cover and how to run each category |
| [DEPLOYMENT_OPTIONS.md](DEPLOYMENT_OPTIONS.md) | Running it online: full feature on one machine, or the free-tier variant |
| [LOCAL_SETUP.md](LOCAL_SETUP.md) | Running it on a laptop |

## Reference

| Document | Contents |
|---|---|
| [API_SPECIFICATION.md](API_SPECIFICATION.md) | Every `/api/v1` endpoint, error codes |
| [DATA_MODEL.md](DATA_MODEL.md) | Tables, constraints, the document state machine |
| [FRONTEND.md](FRONTEND.md) | The Streamlit UI and its session handling |
| [OBSERVABILITY.md](OBSERVABILITY.md) | Request ids, structured logs, OpenTelemetry traces |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Docker images, compose, model and provider settings |
| [HOSTING.md](HOSTING.md) | Paid managed-hosting reference (Render + Supabase) |
| [LICENSES.md](LICENSES.md) | Third-party licences |

## Engineering record (how it was built)

The project was built in phases with quality gates; these files are the working record, kept
deliberately honest (failures and fixes included).

| Document | Contents |
|---|---|
| [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md) | Every significant decision with its reasons (ADR-001 onwards) |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | What is not done or not verified, and what to do about it |
| [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) | Phase and gate status |
| [EXECUTION_LOG.md](EXECUTION_LOG.md) | Dated log of work, commands run and their real results |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md), [plans/](plans/) | Plans as written before the work |
| [AUDIT_REPORT.md](AUDIT_REPORT.md), [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) | Deployment-readiness review and its verification |
| [spec/](spec/) | The original requirements |
