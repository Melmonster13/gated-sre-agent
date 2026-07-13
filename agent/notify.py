"""Notifier (DESIGN §9): one function, two backends — a structured log line
(always) and a generic webhook POST when AGENT_NOTIFY_WEBHOOK is set.

Informational only (SPEC §5 gates): the payload carries no tokens and no
approval affordance — acting on a proposal still requires the act-tier
decision endpoint. Delivery is best-effort; a dead webhook never fails a run.
"""

import json
import logging
import urllib.request

log = logging.getLogger("agent.notify")

WEBHOOK_TIMEOUT_SECONDS = 5


def notify(webhook_url, event):
    payload = json.dumps(event, default=str)
    log.info("notify %s", payload)
    if not webhook_url:
        return
    request = urllib.request.Request(
        webhook_url,
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS)
    except Exception as exc:
        log.warning("webhook delivery failed: %s", exc)
