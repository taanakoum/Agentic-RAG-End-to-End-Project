# Routing evaluation

Dataset `routing_v1`: 86 route cases, 11 rewrite-gate cases. No model is called.

**Route accuracy: 100.0%** · rewrite-gate accuracy: 100.0%

| route | support | precision | recall |
|---|---|---|---|
| smalltalk | 32 | 1.00 | 1.00 |
| clarify | 24 | 1.00 | 1.00 |
| kb | 30 | 1.00 | 1.00 |

Confusion (expected->got): clarify->clarify: 24, kb->kb: 30, smalltalk->smalltalk: 32

## Route mismatches

None.

## Rewrite mismatches

None.

Limits: small hand-written set, same author as the rules; it guards intended behaviour (notably that small-talk look-alikes with a real request go to the knowledge base).
