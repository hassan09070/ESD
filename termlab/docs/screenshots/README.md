# Screenshots referenced in REPORT.md

All captures come from the running stack. Times below are UTC; Grafana and Kibana show the
browser's local time (PKT = UTC+5), so 13:44 UTC is 18:44 local. Prometheus keeps 7 days and
the ILM policy keeps indices 7 days, so anything from 2026-09-13 has to be captured before
2026-09-20. Set Grafana's time picker with absolute times (the range menu → "Absolute time range").

| File | Where | Time range (UTC) | What to show |
|---|---|---|---|
| `b2_app_dashboard_smoke.png` | Grafana → termlab / Application | 2026-09-13 13:43:30 → 13:46:30 | whole dashboard; requests/s, spawn p50/p95 by source, roundtrip, bytes/s all non-zero |
| `b3_business_dashboard_queue_demo.png` | Grafana → termlab / Business | 2026-09-13 13:45:00 → 13:47:00 | stat row with Queued requests > 0 and pool utilisation 100 %, "Queue wait p95 and queue length" on the Application dashboard is the companion (optional) |
| `b5_roundtrip_cpu_hog.png` | Grafana → termlab / Application, panel "Terminal roundtrip p50 / p95 / p99 (1m window)" (panel menu → View) | 2026-09-13 14:04:00 → 14:12:00 | p95 steps from ~10 ms to ~50 ms during the middle stage and back |
| `b6_node_dashboard_cpu_hog.png` | Grafana → termlab / Node Exporter (machine) | 2026-09-13 14:04:00 → 14:12:00 | "Machine" stat showing `docker-desktop`, CPU busy % at 100 during the fault, load average peaking ~35 |
| `c4_kibana_session_trace.png` | Kibana → Discover, data view **termlab logs** | 2026-09-13 13:41:00 → 13:42:00 | query `session_id : "386ed51a5c39"`; add columns event, source, spawn_ms, queue_ms, reason; sort ascending; expand the `sandbox_spawn` document so its fields are visible |
| `d1_architecture.png` | `docs/architecture.md` rendered (GitHub preview or VS Code Markdown preview with Mermaid) | – | the whole diagram |
| `d2_metrics_endpoint.png` | http://localhost:8000/metrics in the browser, or `curl -s localhost:8000/metrics \| grep sessions_started` | now | the `termlab_sessions_started_total{outcome="ok"}` line (the value is whatever the counter is at capture time; the report quotes 82 from the walkthrough) |
| `d2_prometheus_graph.png` | Prometheus → Graph, query `termlab_sessions_started_total{outcome="ok"}` | last 24 h | the staircase with resets at each api restart |
| `d2_grafana_panel.png` | Grafana → termlab / Business, panel "Sessions started by outcome (per minute)" (View) | 2026-09-13 13:43:00 → 13:47:00 | the ok series rising during smoke + queue demo |
| `e1a_spawn_http_p95_cold_start.png` | Grafana → termlab / Application, panels "Sandbox spawn p50 / p95 by source" and "HTTP p95 by route" (top row; crop to those two) | 2026-09-13 13:47:00 → 13:55:00 | cold p95 at ~3 s only in the middle stage; POST /sessions/{id}/sandbox p95 rising by ~2 s, other routes flat |
| `e1a_kibana_spawn_ms_cold_start.png` | Kibana → Discover | 2026-09-13 13:47:00 → 13:55:00 | query `event : "sandbox_spawn" and spawn_ms > 2000`; the histogram shows hits only between 13:50 and 13:52, count 100 |
| `e2_prometheus_count_demo_requests.png` | Prometheus → Graph, query `count(termlab_demo_requests_total)` | 2026-09-13 14:11:00 → 14:14:30 | 1 → (gap) → 100 → 1; optionally a second line `count(last_over_time(termlab_demo_requests_total[15m]))` staying at 101 |
