# 05 Agents, Response Contract, Streaming, Sessions, Concurrency, Memory

## Single-agent (build and verify FIRST)
LangGraph with explicit typed state, nodes, transitions, termination. Nodes: input validation -> query classification -> retrieval-necessity -> rewrite/expand (when useful) -> hybrid retrieval -> rerank -> context construction -> evidence analysis -> grounded generation -> citation verification -> Pydantic validation -> persistence.
Routes: general chat (no retrieval), KB question, tool use, clarification, insufficient evidence. No uncontrolled loops; each node has defined inputs/outputs/failure behavior. Never make unsupported claims when evidence is missing.

## Multi-agent (only after single-agent is VERIFIED)
Supervisor-based LangGraph. Roles (adjust only if simpler is demonstrably better): Supervisor/Planner, Retrieval, Research/Analysis, Answer Synthesis, Critic/Verification. Explicit communication contracts, typed Pydantic state/outputs, max iterations, timeouts, bounded retries, no infinite delegation, no redundant LLM calls, per-agent cost tracking, user authorization preserved across all transitions/tool calls. Same final `RAGResponse`. Benchmark vs single-agent; justify extra latency/cost/complexity with measurements, do not assume it is better.

## RAGResponse (versioned; improve as needed)
```python
class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str; document_name: str
    page_number: int | None = None
    chunk_id: str; excerpt: str

class AgentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_mode: Literal["single","multi"]
    model_name: str; retrieval_used: bool; reranking_used: bool

class RAGResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    insufficient_evidence: bool = False
    clarification_needed: bool = False
    follow_up_questions: list[str] = Field(default_factory=list)
    metadata: AgentMetadata
```
Same contract for single, multi, non-streaming, and final streaming result. Use provider-native structured output where available; handle malformed JSON/missing/invalid/extra fields; **bounded** repair/retries; safe fallback; never expose unvalidated model output. Pydantic validity != truthful citations (see 04).

## Streaming (SSE)
Typed event models: start, token, status, citation, error, complete. `complete` carries/references the validated RAGResponse. Handle partial generation, cancellation, timeouts, client disconnect, provider errors, persistence failure. A response is complete only when the validated result AND required durable state are persisted; document behavior when generation succeeds but persistence fails.

## Sessions and checkpoints
CRUD sessions + messages in PostgreSQL. Persistent PostgreSQL-backed LangGraph checkpointer (never in-memory in the finished app). Explicit mapping user_id <-> session_id <-> thread_id <-> execution_id <-> message_id. Ownership enforced at every access. Define deletion semantics for checkpoints/messages. Conversations survive restart of all containers.

## Same-thread concurrency (explicit)
Choose and document: PG advisory lock per thread, row lock/optimistic version, atomic message insert with sequence numbers, or per-thread queue. Define outcome for a conflicting request: reject (409 CONCURRENCY_CONFLICT) or serialize with documented queue/timeout. Unique thread IDs alone are not protection. Cover: concurrent messages in one conversation, duplicate submit, simultaneous streams, deletion during generation, message ordering, checkpoint overwrite. Different conversations (even same user) must run concurrently.

## Chat idempotency
Client-supplied idempotency key (unique per session). Retries must not create duplicate messages, executions, or responses. Persist message/execution state transitions.

## Memory
Separate from chat history. Types: semantic (stated facts/preferences), episodic, conversation summaries. Store persistently; retrieve only relevant memories; don't inject into every query; don't save every message; controlled consolidation (Celery); users can view/edit/delete/reset; respect consent and retention; no sensitive data by default. Decide PostgreSQL vs Qdrant vs both and document (suggested: PG source of truth + Qdrant vector). Delete removes DB row AND vector. Isolated per user. Test persistence across sessions and restarts.
