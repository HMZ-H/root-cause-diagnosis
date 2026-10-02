"""Root-cause diagnosis tasks: name the underlying cause, not the visible symptom."""

import json
import re
from collections.abc import Iterable

import verifiers.v1 as vf

SYSTEM_PROMPT = """You are debugging a production incident.

You are given the reported symptom and the evidence available. Identify the
ROOT CAUSE - the thing that, if fixed, stops this recurring. Do not stop at the
symptom.

Reply with JSON only:
{"root_cause": "<short id from the candidates list>", "why": "<one sentence>"}"""


CASES = [
    {
        "name": "cert-renewal-failure",
        "symptom": "Outbound mail to external domains has been failing for 48 hours. Internal service-to-service mail is unaffected. No alerts fired.",
        "evidence": [
            "Remote MTAs reject the connection with a certificate verification error.",
            "The outbound relay TLS client certificate expired 48 hours ago.",
            "cert-manager shows the Certificate resource in Failed renewal state for 6 days.",
            "The ACME DNS solver references a Kubernetes secret that does not hold the DNS provider credentials.",
        ],
        "candidates": [
            "expired-tls-cert",
            "certmanager-solver-misconfig",
            "network-firewall-block",
            "mail-queue-overflow",
        ],
        "root_cause": "certmanager-solver-misconfig",
        "symptom_answer": "expired-tls-cert",
    },
    {
        "name": "silent-queue-growth",
        "symptom": "Users report password reset emails never arrive, but the mail service is healthy and the dashboard shows no errors.",
        "evidence": [
            "The spool queue holds 2,147 undelivered messages.",
            "Retry attempts are logged at DEBUG level and the deployed log level is INFO.",
            "The Prometheus PodMonitor targets port 9100; the service exposes metrics on 9154.",
            "The Grafana panel for queue depth reads 'No Data'.",
        ],
        "candidates": [
            "queue-depth-metric-never-scraped",
            "grafana-panel-broken",
            "mail-service-down",
            "users-mistyped-addresses",
        ],
        "root_cause": "queue-depth-metric-never-scraped",
        "symptom_answer": "grafana-panel-broken",
    },
    {
        "name": "n-plus-one-latency",
        "symptom": "The orders list endpoint times out at 30s once a customer has more than ~400 orders. Smaller accounts are fine.",
        "evidence": [
            "The handler loops over orders and loads each order's line items individually.",
            "pg_stat_statements shows 412 near-identical SELECTs on order_items per request.",
            "EXPLAIN on the line-item query reports a sequential scan; order_items has no index on order_id.",
            "CPU on the database sits at 95% during the request.",
        ],
        "candidates": [
            "missing-index-on-order-items",
            "n-plus-one-query-pattern",
            "database-undersized",
            "request-timeout-too-low",
        ],
        "root_cause": "n-plus-one-query-pattern",
        "symptom_answer": "request-timeout-too-low",
    },
    {
        "name": "order-dependent-test",
        "symptom": "CI fails about one run in five with a single assertion error in test_user_quota. Re-running the job passes.",
        "evidence": [
            "pytest-randomly is enabled, so test order varies per run.",
            "test_admin_seed writes a row to the shared users table and does not roll back.",
            "test_user_quota counts rows in users and asserts the count equals 3.",
            "Both tests share one database session fixture scoped to the module.",
        ],
        "candidates": [
            "flaky-network",
            "shared-fixture-leaks-state",
            "assertion-value-wrong",
            "ci-runner-too-slow",
        ],
        "root_cause": "shared-fixture-leaks-state",
        "symptom_answer": "assertion-value-wrong",
    },
    {
        "name": "cache-stampede",
        "symptom": "Every day at 03:00 the API latency spikes to 8s for about 90 seconds, then recovers on its own.",
        "evidence": [
            "The product catalogue cache is written with a fixed 24h TTL by a job that runs at 03:00 the previous day.",
            "All cache keys therefore expire within the same second.",
            "On a miss the handler recomputes the catalogue from the database with no lock.",
            "Database connection pool saturation is logged during the spike.",
        ],
        "candidates": [
            "simultaneous-ttl-expiry-stampede",
            "database-too-small",
            "nightly-backup-contention",
            "memory-leak-in-api",
        ],
        "root_cause": "simultaneous-ttl-expiry-stampede",
        "symptom_answer": "database-too-small",
    },

    {
        "name": "connection-pool-exhaustion",
        "symptom": "The API returns 500s under moderate load. Restarting the service fixes it for a few hours, then it returns.",
        "evidence": [
            "Errors are 'timeout acquiring connection from pool'; pool size is 20.",
            "The database reports 18 idle-in-transaction connections held for over an hour.",
            "One handler opens a connection, and on the error path returns before the context manager is entered.",
            "The error path is hit roughly 2% of requests, matching the time to exhaustion.",
        ],
        "candidates": ["pool-size-too-small", "connection-leaked-on-error-path", "database-overloaded", "network-timeouts"],
        "root_cause": "connection-leaked-on-error-path",
        "symptom_answer": "pool-size-too-small",
    },
    {
        "name": "clock-skew-auth",
        "symptom": "About one request in six fails with 401 invalid token. Retrying usually succeeds. Started after adding a third API node.",
        "evidence": [
            "Tokens are signed with a 60-second not-before claim and validated on every node.",
            "The new node's system clock is 94 seconds ahead of the other two.",
            "chronyd is installed on the new node but not enabled.",
            "Failures correlate exactly with requests routed to the new node.",
        ],
        "candidates": ["tokens-expiring-too-fast", "ntp-not-running-on-new-node", "load-balancer-misrouting", "token-secret-mismatch"],
        "root_cause": "ntp-not-running-on-new-node",
        "symptom_answer": "tokens-expiring-too-fast",
    },
    {
        "name": "disk-filled-by-logs",
        "symptom": "The service crashes most nights between 02:00 and 04:00 and comes back after the node is restarted.",
        "evidence": [
            "The crash is preceded by 'no space left on device' when writing to /var/log.",
            "logrotate is installed but the config for this service sets size 0 and no rotation schedule.",
            "A nightly batch job writes roughly 40 GB of debug output between 01:00 and 03:00.",
            "Disk usage drops to zero only because the node restart recreates an ephemeral volume.",
        ],
        "candidates": ["disk-too-small", "log-rotation-never-configured", "batch-job-memory-leak", "node-unstable"],
        "root_cause": "log-rotation-never-configured",
        "symptom_answer": "disk-too-small",
    },
    {
        "name": "stale-dns-after-failover",
        "symptom": "After failing over to the standby database, roughly a third of application pods kept writing to the old primary for 40 minutes.",
        "evidence": [
            "The database endpoint is a CNAME; failover updated it within 15 seconds.",
            "The record is published with a TTL of 3600 seconds.",
            "The application's connection library resolves the hostname once at startup and caches it for the process lifetime.",
            "Pods that restarted during the window picked up the new address immediately.",
        ],
        "candidates": ["failover-did-not-complete", "client-caches-dns-for-process-lifetime", "standby-was-not-ready", "network-partition"],
        "root_cause": "client-caches-dns-for-process-lifetime",
        "symptom_answer": "failover-did-not-complete",
    },
    {
        "name": "transitive-dependency-break",
        "symptom": "CI started failing on main this morning. Nobody merged anything in the last three days.",
        "evidence": [
            "The failure is an ImportError on a symbol that existed yesterday.",
            "The direct dependency is pinned to ==2.4.1 in requirements.txt.",
            "That package declares its own dependency as >=1.0, and 1.9.0 was released six hours ago removing the symbol.",
            "There is no lock file; CI resolves dependencies fresh on every run.",
        ],
        "candidates": ["upstream-library-broke-api", "transitive-deps-unpinned-no-lockfile", "ci-cache-corrupted", "python-version-changed"],
        "root_cause": "transitive-deps-unpinned-no-lockfile",
        "symptom_answer": "upstream-library-broke-api",
    },
    {
        "name": "naive-datetime-month-end",
        "symptom": "The monthly revenue report is short by a few hundred rows, but only for months ending on the 31st.",
        "evidence": [
            "Rows are written with timezone-aware UTC timestamps.",
            "The report query builds its range with datetime.now() and no timezone, which resolves to the server's local time of UTC+3.",
            "The missing rows all fall in the last three hours of the final day.",
            "Months ending on the 30th show the same gap but it lands inside the next month's report, so nobody noticed.",
        ],
        "candidates": ["query-date-range-off-by-one", "naive-and-aware-datetimes-mixed", "rows-not-written", "report-caching"],
        "root_cause": "naive-and-aware-datetimes-mixed",
        "symptom_answer": "query-date-range-off-by-one",
    },
    {
        "name": "retry-amplification",
        "symptom": "One slow downstream service took the entire cluster down. It had only degraded to 2s responses, not failed.",
        "evidence": [
            "The calling service retries 3 times with no backoff and no jitter.",
            "Two services sit between the entry point and the slow one, each retrying 3 times.",
            "A single user request therefore produced up to 27 downstream calls.",
            "Request volume at the slow service rose 24x while entry-point traffic was flat.",
        ],
        "candidates": ["downstream-service-too-slow", "retries-without-backoff-amplify-load", "insufficient-replicas", "load-balancer-misconfigured"],
        "root_cause": "retries-without-backoff-amplify-load",
        "symptom_answer": "downstream-service-too-slow",
    },
    {
        "name": "missing-idempotency-key",
        "symptom": "A small number of customers were charged twice for the same order. Support confirms they only pressed pay once.",
        "evidence": [
            "The payment call has a 10-second client timeout; the provider sometimes answers in 12 seconds.",
            "On timeout the client retries the same request.",
            "The request carries no idempotency key, so the provider treats each attempt as a new charge.",
            "Provider logs show two successful authorisations seconds apart for each affected order.",
        ],
        "candidates": ["users-double-clicking", "no-idempotency-key-on-retry", "provider-duplicate-bug", "timeout-too-short"],
        "root_cause": "no-idempotency-key-on-retry",
        "symptom_answer": "timeout-too-short",
    },
    {
        "name": "unbounded-in-process-cache",
        "symptom": "Pods are OOM-killed after about four days. Memory climbs steadily from deploy and never falls.",
        "evidence": [
            "A module-level dict caches rendered templates keyed by the full request path.",
            "Request paths include a unique query parameter, so every request creates a new key.",
            "The dict has no maximum size and no eviction.",
            "Heap dumps show that dict holding 91% of resident memory at the time of the kill.",
        ],
        "candidates": ["memory-limit-too-low", "cache-with-no-eviction-policy", "garbage-collector-tuning", "memory-leak-in-dependency"],
        "root_cause": "cache-with-no-eviction-policy",
        "symptom_answer": "memory-limit-too-low",
    },
    {
        "name": "read-after-write-replica-lag",
        "symptom": "Users save their profile, the page reloads, and the old values are shown. Refreshing again a few seconds later shows the new ones.",
        "evidence": [
            "Writes go to the primary; all reads are routed to a read replica.",
            "Replica lag averages 400ms and spikes to 3s during the nightly batch window.",
            "The redirect after save issues the read within 50ms of the write committing.",
            "There is no cache in front of the read path.",
        ],
        "candidates": ["browser-caching-the-page", "read-after-write-hits-lagging-replica", "save-not-persisting", "session-state-stale"],
        "root_cause": "read-after-write-hits-lagging-replica",
        "symptom_answer": "save-not-persisting",
    },
    {
        "name": "swallowed-exception",
        "symptom": "About 4% of uploaded records never appear in the warehouse. No errors are logged and the job reports success every run.",
        "evidence": [
            "The per-record loop is wrapped in a bare except Exception: continue.",
            "The job's success criterion is that the loop completes, not that the row count matches.",
            "Adding a counter to the except branch shows it firing on exactly the missing records.",
            "The underlying failure is a unicode decode error on one supplier's file encoding.",
        ],
        "candidates": ["supplier-file-encoding-wrong", "exceptions-swallowed-so-failures-invisible", "warehouse-dropping-rows", "job-timing-out"],
        "root_cause": "exceptions-swallowed-so-failures-invisible",
        "symptom_answer": "supplier-file-encoding-wrong",
    },
    {
        "name": "shallow-health-check",
        "symptom": "The load balancer kept sending traffic to a pod that returned errors for every request for 20 minutes.",
        "evidence": [
            "The liveness probe is an HTTP GET on /healthz which returns 200 if the process is up.",
            "/healthz does not touch the database or any downstream dependency.",
            "The pod had lost its database connection and could not reconnect.",
            "Every real request to that pod returned 500 while the probe stayed green.",
        ],
        "candidates": ["pod-lost-database-connection", "health-check-does-not-test-dependencies", "load-balancer-misconfigured", "probe-interval-too-long"],
        "root_cause": "health-check-does-not-test-dependencies",
        "symptom_answer": "pod-lost-database-connection",
    },
    {
        "name": "migration-lock-timeout",
        "symptom": "The deploy hung for 15 minutes at the migration step, then rolled back. The migration adds one nullable column.",
        "evidence": [
            "ALTER TABLE on that table requires a brief ACCESS EXCLUSIVE lock.",
            "An analytics query opened a transaction 40 minutes earlier and is still holding a lock on the same table.",
            "pg_locks shows the migration waiting behind that transaction.",
            "The migration itself completes in under 50ms when run against an idle database.",
        ],
        "candidates": ["migration-too-slow", "long-running-transaction-holds-lock", "database-undersized", "deploy-timeout-too-short"],
        "root_cause": "long-running-transaction-holds-lock",
        "symptom_answer": "migration-too-slow",
    },

    {
        "name": "connection-pool-exhaustion",
        "symptom": "The API returns 500s under moderate load. Restarting the service fixes it for a few hours, then it returns.",
        "evidence": [
            "Errors are 'timeout acquiring connection from pool'; pool size is 20.",
            "The database reports 18 idle-in-transaction connections held for over an hour.",
            "One handler opens a connection, and on the error path returns before the context manager is entered.",
            "The error path is hit roughly 2% of requests, matching the time to exhaustion.",
        ],
        "candidates": ["pool-size-too-small", "connection-leaked-on-error-path", "database-overloaded", "network-timeouts"],
        "root_cause": "connection-leaked-on-error-path",
        "symptom_answer": "pool-size-too-small",
    },
    {
        "name": "clock-skew-auth",
        "symptom": "About one request in six fails with 401 invalid token. Retrying usually succeeds. Started after adding a third API node.",
        "evidence": [
            "Tokens are signed with a 60-second not-before claim and validated on every node.",
            "The new node's system clock is 94 seconds ahead of the other two.",
            "chronyd is installed on the new node but not enabled.",
            "Failures correlate exactly with requests routed to the new node.",
        ],
        "candidates": ["tokens-expiring-too-fast", "ntp-not-running-on-new-node", "load-balancer-misrouting", "token-secret-mismatch"],
        "root_cause": "ntp-not-running-on-new-node",
        "symptom_answer": "tokens-expiring-too-fast",
    },
    {
        "name": "disk-filled-by-logs",
        "symptom": "The service crashes most nights between 02:00 and 04:00 and comes back after the node is restarted.",
        "evidence": [
            "The crash is preceded by 'no space left on device' when writing to /var/log.",
            "logrotate is installed but the config for this service sets size 0 and no rotation schedule.",
            "A nightly batch job writes roughly 40 GB of debug output between 01:00 and 03:00.",
            "Disk usage drops to zero only because the node restart recreates an ephemeral volume.",
        ],
        "candidates": ["disk-too-small", "log-rotation-never-configured", "batch-job-memory-leak", "node-unstable"],
        "root_cause": "log-rotation-never-configured",
        "symptom_answer": "disk-too-small",
    },
    {
        "name": "stale-dns-after-failover",
        "symptom": "After failing over to the standby database, roughly a third of application pods kept writing to the old primary for 40 minutes.",
        "evidence": [
            "The database endpoint is a CNAME; failover updated it within 15 seconds.",
            "The record is published with a TTL of 3600 seconds.",
            "The application's connection library resolves the hostname once at startup and caches it for the process lifetime.",
            "Pods that restarted during the window picked up the new address immediately.",
        ],
        "candidates": ["failover-did-not-complete", "client-caches-dns-for-process-lifetime", "standby-was-not-ready", "network-partition"],
        "root_cause": "client-caches-dns-for-process-lifetime",
        "symptom_answer": "failover-did-not-complete",
    },
    {
        "name": "transitive-dependency-break",
        "symptom": "CI started failing on main this morning. Nobody merged anything in the last three days.",
        "evidence": [
            "The failure is an ImportError on a symbol that existed yesterday.",
            "The direct dependency is pinned to ==2.4.1 in requirements.txt.",
            "That package declares its own dependency as >=1.0, and 1.9.0 was released six hours ago removing the symbol.",
            "There is no lock file; CI resolves dependencies fresh on every run.",
        ],
        "candidates": ["upstream-library-broke-api", "transitive-deps-unpinned-no-lockfile", "ci-cache-corrupted", "python-version-changed"],
        "root_cause": "transitive-deps-unpinned-no-lockfile",
        "symptom_answer": "upstream-library-broke-api",
    },
    {
        "name": "naive-datetime-month-end",
        "symptom": "The monthly revenue report is short by a few hundred rows, but only for months ending on the 31st.",
        "evidence": [
            "Rows are written with timezone-aware UTC timestamps.",
            "The report query builds its range with datetime.now() and no timezone, which resolves to the server's local time of UTC+3.",
            "The missing rows all fall in the last three hours of the final day.",
            "Months ending on the 30th show the same gap but it lands inside the next month's report, so nobody noticed.",
        ],
        "candidates": ["query-date-range-off-by-one", "naive-and-aware-datetimes-mixed", "rows-not-written", "report-caching"],
        "root_cause": "naive-and-aware-datetimes-mixed",
        "symptom_answer": "query-date-range-off-by-one",
    },
    {
        "name": "retry-amplification",
        "symptom": "One slow downstream service took the entire cluster down. It had only degraded to 2s responses, not failed.",
        "evidence": [
            "The calling service retries 3 times with no backoff and no jitter.",
            "Two services sit between the entry point and the slow one, each retrying 3 times.",
            "A single user request therefore produced up to 27 downstream calls.",
            "Request volume at the slow service rose 24x while entry-point traffic was flat.",
        ],
        "candidates": ["downstream-service-too-slow", "retries-without-backoff-amplify-load", "insufficient-replicas", "load-balancer-misconfigured"],
        "root_cause": "retries-without-backoff-amplify-load",
        "symptom_answer": "downstream-service-too-slow",
    },
    {
        "name": "missing-idempotency-key",
        "symptom": "A small number of customers were charged twice for the same order. Support confirms they only pressed pay once.",
        "evidence": [
            "The payment call has a 10-second client timeout; the provider sometimes answers in 12 seconds.",
            "On timeout the client retries the same request.",
            "The request carries no idempotency key, so the provider treats each attempt as a new charge.",
            "Provider logs show two successful authorisations seconds apart for each affected order.",
        ],
        "candidates": ["users-double-clicking", "no-idempotency-key-on-retry", "provider-duplicate-bug", "timeout-too-short"],
        "root_cause": "no-idempotency-key-on-retry",
        "symptom_answer": "timeout-too-short",
    },
    {
        "name": "unbounded-in-process-cache",
        "symptom": "Pods are OOM-killed after about four days. Memory climbs steadily from deploy and never falls.",
        "evidence": [
            "A module-level dict caches rendered templates keyed by the full request path.",
            "Request paths include a unique query parameter, so every request creates a new key.",
            "The dict has no maximum size and no eviction.",
            "Heap dumps show that dict holding 91% of resident memory at the time of the kill.",
        ],
        "candidates": ["memory-limit-too-low", "cache-with-no-eviction-policy", "garbage-collector-tuning", "memory-leak-in-dependency"],
        "root_cause": "cache-with-no-eviction-policy",
        "symptom_answer": "memory-limit-too-low",
    },
    {
        "name": "read-after-write-replica-lag",
        "symptom": "Users save their profile, the page reloads, and the old values are shown. Refreshing again a few seconds later shows the new ones.",
        "evidence": [
            "Writes go to the primary; all reads are routed to a read replica.",
            "Replica lag averages 400ms and spikes to 3s during the nightly batch window.",
            "The redirect after save issues the read within 50ms of the write committing.",
            "There is no cache in front of the read path.",
        ],
        "candidates": ["browser-caching-the-page", "read-after-write-hits-lagging-replica", "save-not-persisting", "session-state-stale"],
        "root_cause": "read-after-write-hits-lagging-replica",
        "symptom_answer": "save-not-persisting",
    },
    {
        "name": "swallowed-exception",
        "symptom": "About 4% of uploaded records never appear in the warehouse. No errors are logged and the job reports success every run.",
        "evidence": [
            "The per-record loop is wrapped in a bare except Exception: continue.",
            "The job's success criterion is that the loop completes, not that the row count matches.",
            "Adding a counter to the except branch shows it firing on exactly the missing records.",
            "The underlying failure is a unicode decode error on one supplier's file encoding.",
        ],
        "candidates": ["supplier-file-encoding-wrong", "exceptions-swallowed-so-failures-invisible", "warehouse-dropping-rows", "job-timing-out"],
        "root_cause": "exceptions-swallowed-so-failures-invisible",
        "symptom_answer": "supplier-file-encoding-wrong",
    },
    {
        "name": "shallow-health-check",
        "symptom": "The load balancer kept sending traffic to a pod that returned errors for every request for 20 minutes.",
        "evidence": [
            "The liveness probe is an HTTP GET on /healthz which returns 200 if the process is up.",
            "/healthz does not touch the database or any downstream dependency.",
            "The pod had lost its database connection and could not reconnect.",
            "Every real request to that pod returned 500 while the probe stayed green.",
        ],
        "candidates": ["pod-lost-database-connection", "health-check-does-not-test-dependencies", "load-balancer-misconfigured", "probe-interval-too-long"],
        "root_cause": "health-check-does-not-test-dependencies",
        "symptom_answer": "pod-lost-database-connection",
    },
    {
        "name": "migration-lock-timeout",
        "symptom": "The deploy hung for 15 minutes at the migration step, then rolled back. The migration adds one nullable column.",
        "evidence": [
            "ALTER TABLE on that table requires a brief ACCESS EXCLUSIVE lock.",
            "An analytics query opened a transaction 40 minutes earlier and is still holding a lock on the same table.",
            "pg_locks shows the migration waiting behind that transaction.",
            "The migration itself completes in under 50ms when run against an idle database.",
        ],
        "candidates": ["migration-too-slow", "long-running-transaction-holds-lock", "database-undersized", "deploy-timeout-too-short"],
        "root_cause": "long-running-transaction-holds-lock",
        "symptom_answer": "migration-too-slow",
    }
]

CASES = list({c["name"]: c for c in CASES}.values())


def _parse(reply: str) -> dict:
    """Pull the JSON object out of a reply that may be wrapped in prose or fences."""
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class RootCauseData(vf.TaskData):
    candidates: list[str]
    root_cause: str
    symptom_answer: str


class RootCauseTask(vf.Task[RootCauseData]):
    @vf.reward(weight=1.0)
    async def correct_root_cause(self, trace: vf.Trace) -> float:
        """Full credit only for the underlying cause."""
        answer = _parse(trace.last_reply).get("root_cause", "")
        return float(str(answer).strip().lower() == self.data.root_cause.lower())

    @vf.reward(weight=-0.5)
    async def stopped_at_symptom(self, trace: vf.Trace) -> float:
        """Penalty for naming the visible symptom instead of the cause."""
        answer = _parse(trace.last_reply).get("root_cause", "")
        return float(str(answer).strip().lower() == self.data.symptom_answer.lower())

    @vf.reward(weight=0.1)
    async def valid_format(self, trace: vf.Trace) -> float:
        """Small credit for answering in the requested shape at all."""
        parsed = _parse(trace.last_reply)
        return float(bool(parsed.get("root_cause")) and bool(parsed.get("why")))


class RootCauseTaskset(vf.Taskset[RootCauseTask, vf.TasksetConfig]):
    def load(self) -> Iterable[RootCauseTask]:
        for i, case in enumerate(CASES):
            evidence = "\n".join(f"- {line}" for line in case["evidence"])
            options = ", ".join(case["candidates"])
            prompt = (
                f"Reported symptom:\n{case['symptom']}\n\n"
                f"Evidence:\n{evidence}\n\n"
                f"Candidate causes: {options}"
            )
            yield RootCauseTask(
                RootCauseData(
                    idx=i,
                    name=case["name"],
                    prompt=prompt,
                    system_prompt=SYSTEM_PROMPT,
                    candidates=case["candidates"],
                    root_cause=case["root_cause"],
                    symptom_answer=case["symptom_answer"],
                ),
                self.config.task,
            )


__all__ = ["RootCauseTaskset"]
