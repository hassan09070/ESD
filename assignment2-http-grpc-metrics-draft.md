# Assignment 2: HTTP APIs, gRPC, and Metrics

Enterprise Software Development, Fall 2026

Individual assignment. Extend your Assignment 1 project. Dates and submission details will follow. No load test is required.

## Part A: Improve the HTTP API — 40 marks

### REST principles

In a short table, write the six REST constraints covered in [the lecture](../lectures/APIs.md#74-the-rest-constraints). Mark code on demand as optional. For each, give a plain-language meaning and one example from your API, or say that your API does not demonstrate it. You do not need to implement all six.

### Apply the practices

Review all application endpoints (excluding health and metrics). Use clear paths and appropriate HTTP methods, validate input, and return meaningful status codes with a consistent error format.

Also apply these practices:

1. **OpenAPI Specification:** Look up what it is and how it is used. Explain its purpose in 2–3 sentences. Create a spec for your HTTP API that matches its routes, inputs, and success/error responses. Use it to inspect or test at least one endpoint.
2. **Idempotency (safe repeats):** For an operation that may be retried, prevent duplicate effects or explain why repeating it is safe.
3. **Rate limiting:** Limit calls to one HTTP endpoint (a single-instance implementation is fine). Show accepted requests and a `429 Too Many Requests` response with `Retry-After`. State the limit, who shares it, and when calls are allowed again.

Pick one **user task** for a brief before/after table: old endpoint, problem, change, and reason. List the other endpoints you changed or left alone. Update callers affected by your changes.
Test a valid request, invalid input, and a repeated request.

Write a small **Python caller** for one of your HTTP endpoints. It acts like another service calling your API; you do not need to build another server. Have it make several calls in one run, and add:

- A timeout.
- A limited retry with backoff for a transient failure. Retry only when the operation is safe or protected against duplicates.
- A circuit breaker that stops calls after repeated failures and tries again after a recovery period.

Show the caller succeeding, retrying a failure, blocking calls while the breaker is open, and recovering. Include its output and state your timeout, retry limit, and breaker settings. Use a timeout or server failure for this test; demonstrate rate limiting separately.

## Part B: Add a gRPC service — 30 marks

Take **one operation** from the chosen task and implement it in a new process/container as a gRPC service. Keep the HTTP version. Both must use the same business rules and source of truth; a gRPC wrapper around the old HTTP endpoint does not count.

- Define the RPC and messages in `.proto`; generate the code and implement the handler.
- Write a Python client (or extend your Part A script) that calls the gRPC service directly, accepts input, sets a deadline, and reports results or errors. No browser is needed.
- Show one successful and one failed request through both versions. Explain the HTTP and gRPC statuses.
- Draw both call paths, including the generated stub and server handler.
- Keep A1 logs and trace correlation working across the gRPC boundary.

Unary RPC is enough unless streaming suits the operation. Do not repeat A1's logging probes.

## Part C: Extend the metrics — 30 marks

Audit your A1 metrics: what is useful, what is missing, and what you will fix or add. Do not repeat the four-metric-type exercise.

Prometheus must scrape the new gRPC service. In Grafana, show its metrics beside those for the HTTP operation:

- Calls and error rate by RPC method and status.
- Duration histogram with suitable buckets; show p50 and p95 over a stated time window.
- Either in-flight calls or message size; explain your choice.
- Any other missing project metric you identify, or explain why none is needed.

Generate a few repeatable success and failure calls to populate the panels. For each new metric, give its purpose, type, unit, labels, code location, and query. Say whether duration is measured at the client or server. Use bounded labels; never use user, request, or trace IDs as labels. The Python caller does not need a Prometheus endpoint.

## Submit

- Code, OpenAPI document, `.proto`, generation command, Python client, tests, Prometheus configuration, and Grafana dashboard.
- README with run and test commands.
- Short report: REST table, OpenAPI explanation and use, API before/after table, rate-limit and reliability test results, call-path diagram, HTTP/gRPC examples, metric gap table, queries, and dashboard evidence.

You may use [the gRPC workshop](../../labs/week1-midnight-launch/GRPC-WORKSHOP.md) as a reference. Cite reused work. Exclude data volumes. Follow the course academic-integrity policy.
