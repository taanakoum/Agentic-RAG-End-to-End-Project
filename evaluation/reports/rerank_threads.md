# Cross-encoder ONNX threads (interleaved measurement)

Model `BAAI/bge-reranker-v2-m3-int8`, 5 candidates of up to 2000 characters, 8 logical cores (Intel64 Family 6 Model 140 Stepping 1, GenuineIntel); every setting was called in round-robin order, 6 rounds x 8 questions, after a warm-up call.

| ONNX threads | calls | p50 ms | p95 ms |
|---|---|---|---|
| default | 48 | 4106 | 5327 |
| 2 | 48 | 5314 | 7986 |
| 4 | 48 | 4125 | 5669 |
| 8 | 48 | 4376 | 6774 |

One laptop, one run; the absolute numbers depend on load and power settings.
