---
name: rag-citation-and-response
description: Implement and review the versioned RAGResponse contract, bounded structured-output repair, server-side citation verification, SSE event schemas, and prompt-injection-safe context construction. Use when working on agents, generation, chat endpoints, or streaming.
---

# Response, citation, streaming

- One versioned `RAGResponse` for single, multi, non-stream and final stream. `extra="forbid"`. See `docs/spec/05-agents-chat-memory.md`.
- Generation: provider-native structured output when available; on parse/validation failure do at most N (configurable, small) repair attempts; then return a safe fallback (e.g. insufficient_evidence=True) rather than raw model output.
- **Citation verification (server-side, every citation)**: document exists, chunk exists, chunk belongs to document, user owns the document, page matches stored metadata, chunk id is in the evidence set retrieved in this execution. Excerpt must be validated against stored chunk text. Drop or correct invalid ones (bounded); if none remain for a factual answer, mark insufficient_evidence.
- Distinguish supported / inference / not-found in the answer metadata or text policy.
- Context construction: retrieved text is untrusted data inside clearly delimited blocks; system rules stay outside; never let document text change tools, permissions, or user identity. Tools get the authenticated user context, not model-chosen IDs.
- SSE events (typed Pydantic): start, token, status, citation, error, complete. complete holds the validated response. Handle disconnect (cancel graph, release thread lock, persist partial state per documented rule), timeouts, provider errors, persistence failure (no false success).
- Tests with a deterministic mock LLM: malformed JSON, extra fields, fabricated chunk id, other user's chunk id, wrong page, repeated invalid output (bounded), injection text in a PDF, event ordering.
