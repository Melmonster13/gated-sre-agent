"""Append-only audit log (SPEC §6 audit_log). One JSONL record per run outcome."""

import datetime as dt
import json


def audit(path, record):
    record = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), **record}
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return record
