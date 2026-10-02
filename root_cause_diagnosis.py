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
]


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
