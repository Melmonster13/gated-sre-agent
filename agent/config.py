"""Runtime configuration, from env (SPEC §3 scope, §5 actions, §6 identity)."""

import os
from dataclasses import dataclass

# SPEC §5 actions table. Graduation is a spec amendment, not a code toggle:
# flipping a tier here without amending SPEC.md is wrong by definition.
ACTIONS = {
    "restart_pod": {"implemented": True, "tier": "draft_only"},
    "rollback_deployment": {"implemented": False, "tier": "draft_only"},
    "bump_resources": {"implemented": False, "tier": "draft_only"},
}

PROPOSAL_TTL_SECONDS = 3600  # SPEC §5 timeout_behavior: expires after 1h


def _split(value):
    return [item for item in (part.strip() for part in value.split(",")) if item]


@dataclass(frozen=True)
class Config:
    namespaces_watched: tuple   # SPEC §3 scope.namespaces_watched
    namespaces_write: tuple     # SPEC §6 rbac write allowlist
    model: str
    db_path: str
    audit_path: str
    poll_seconds: int
    verify_wait_seconds: int
    # Test affordance only (DESIGN §2): draft_only actions may cross the gate
    # into Act, but solely in these namespaces. Empty in any real deployment —
    # this is how the walking skeleton and integration tests exercise the Act
    # path before restart_pod graduates per SPEC §5.
    execute_override_namespaces: tuple
    observe_token: str
    act_token: str


def load_config():
    env = os.environ.get
    return Config(
        namespaces_watched=tuple(_split(env("AGENT_NAMESPACES", "default,apps"))),
        namespaces_write=tuple(_split(env("AGENT_WRITE_NAMESPACES", "default,apps"))),
        model=env("LLM_MODEL", "claude-opus-4-8"),
        db_path=env("AGENT_DB", "agent.db"),
        audit_path=env("AGENT_AUDIT", "audit.jsonl"),
        poll_seconds=int(env("AGENT_POLL_SECONDS", "10")),
        verify_wait_seconds=int(env("AGENT_VERIFY_WAIT_SECONDS", "30")),
        execute_override_namespaces=tuple(_split(env("AGENT_TEST_EXECUTE_NAMESPACES", ""))),
        observe_token=env("AGENT_OBSERVE_TOKEN", ""),
        act_token=env("AGENT_ACT_TOKEN", ""),
    )


def executable(action_id, namespace, config):
    """SPEC §5: may this proposal cross the gate into Act?"""
    action = ACTIONS.get(action_id)
    if not action or not action["implemented"]:
        return False
    if action["tier"] == "draft_only":
        return namespace in config.execute_override_namespaces
    return True
