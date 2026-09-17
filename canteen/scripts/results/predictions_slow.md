# Predictions for the E.1 `slow` experiment (written before running it, 2026-09-17)

Fault: `POST /chaos {"slow_every_n": 5, "delay_ms": 500}` — every 5th request to an /orders route sleeps 500 ms. Load: 4 concurrent customers for >= 120 s per stage.

- P1 HTTP p95 (all routes) rises from ~10 ms to ~0.5 s (the bucket edge 0.5 s or the next one, 1 s) during the fault; p50 stays at a few ms because only 20% of requests are delayed; p99 also ~0.5-1 s.
- P2 HTTP mean rises by about 0.2 x 0.5 s = 100 ms.
- P3 Requests/s falls: each delayed customer loop takes longer, so fewer orders per stage (roughly 10-20% fewer). No change in the status mix: no 5xx, no 503 (the fault is slow, not broken).
- P4 Business metrics barely move: orders waiting, prep p95 and pickup delay p95 are dominated by the sleeps in load.py (0.5-3 s), not by 0.5 s of server delay; cancel rate unchanged (10%).
- P5 Kibana: `event:"http_request" and duration_ms > 400` matches ~20% of http_request documents in the fault stage and 0 in baseline/recovery; `level:"error"` stays 0; two `chaos_changed` lines bracket the fault stage (set + reset).
- P6 Recovery: the [1m] latency panels return to baseline within a minute of the reset; increase()-based counts recover to baseline numbers in the recovery stage.
- P7 Node Exporter: no visible CPU change (the fault is a sleep).
