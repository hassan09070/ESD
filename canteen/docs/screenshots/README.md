# Screenshots referenced in REPORT.md

Times are UTC; Grafana and Kibana show the browser's local time (PKT = UTC+5). Prometheus and
the ILM policy keep 7 days, so captures of the 2026-09-17 experiments must be taken before 2026-09-24.
Use Grafana's absolute time range (time picker → "Absolute time range"); Kibana's date picker also accepts absolute times.

| File | Where | Time range (UTC) | What to show |
|---|---|---|---|
| `b_app_dashboard.png` | Grafana → canteen / Application | the E.1 window from `scripts/results/fault_slow_*.txt.stages` (baseline start → recovery end) | the whole dashboard: p95 step up in the middle stage, p50 flat, no error rate |
| `b_business_dashboard.png` | Grafana → canteen / Business | same window | stat row + orders/min by stall + waiting gauge + prep p95 vs prep mean + pickup delay |
| `b_node_dashboard.png` | Grafana → canteen / Node Exporter (machine) | last 1 h | the machine stat showing `docker-desktop`, CPU, memory, disk, network |
| `c_kibana_order_trace.png` | Kibana → Discover, data view **canteen logs** | last 24 h | query `order_id : "<id from scripts/results>"`, columns event / stall / prep_s / pickup_delay_s / request_id, 3 lines placed → ready → picked_up |
| `c_kibana_slow_requests.png` | Kibana → Discover | E.1 window | query `event : "http_request" and duration_ms > 400`; the histogram shows hits only in the fault stage |
| `d_architecture.png` | `docs/architecture.md` rendered (GitHub or VS Code Markdown preview) | – | the diagram |
| `d_metric_walk.png` | three crops: `curl localhost:8000/metrics \| grep orders_placed`; Prometheus → Graph `sum(canteen_orders_placed_total)`; Grafana → Business → "Orders placed per minute, by stall" | last 1 h | the same counter at each step |
| `e1_latency_three_stages.png` | Grafana → canteen / Application, panel "HTTP latency p50 / p95 / p99" (View) | E.1 window | p95 at ~0.5 s only in the middle stage |
| `e2_cardinality.png` | Prometheus → Graph, `count(canteen_demo_requests_total)` and `count(last_over_time(canteen_demo_requests_total[15m]))` | E.2 window from `scripts/results/cardinality_*.json` timestamps | 1 → 100 → 1 for the first, 101 staying for the second |
