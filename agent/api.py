"""FastAPI surface (DESIGN §7): observe/act tiers over the run registry.

Any approval surface is a thin client of POST /proposals/{id}/decision —
the agent doesn't know or care which surface approved (SPEC §5).
"""

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

log = logging.getLogger("agent.api")

PANEL = (Path(__file__).parent / "panel.html").read_text()


class Decision(BaseModel):
    approve: bool
    decided_by: str


def require_tier(tier):
    def check(request: Request):
        config = request.app.state.config
        token = {"observe": config.observe_token, "act": config.act_token}[tier]
        if not token:
            return  # dev mode: no tokens configured (warned at startup)
        header = request.headers.get("authorization", "")
        if header != f"Bearer {token}":
            raise HTTPException(status_code=401, detail=f"{tier}-tier token required")
    return check


def build_app(runtime, config):
    app = FastAPI(title="gated-sre-agent")
    app.state.config = config
    if not (config.observe_token and config.act_token):
        log.warning("AGENT_OBSERVE_TOKEN/AGENT_ACT_TOKEN not set — API is unauthenticated (dev only)")

    @app.get("/", response_class=HTMLResponse)
    def panel():
        """Reference approval panel (DESIGN §8). The page itself is static and
        data-free; every fetch it makes carries the caller's tier token."""
        return PANEL

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/proposals", dependencies=[Depends(require_tier("observe"))])
    def proposals():
        return {tid: run for tid, run in runtime.snapshot().items() if run["status"] == "paused"}

    @app.get("/proposals/{thread_id}", dependencies=[Depends(require_tier("observe"))])
    def proposal(thread_id: str):
        run = runtime.snapshot(thread_id)
        if not run or run["status"] != "paused":
            raise HTTPException(status_code=404, detail="no paused proposal with that id")
        return run

    @app.post("/proposals/{thread_id}/decision", dependencies=[Depends(require_tier("act"))])
    def decide(thread_id: str, decision: Decision):
        if not runtime.resume(thread_id, decision.model_dump()):
            raise HTTPException(status_code=409, detail="run is not paused at the gate")
        return {"resumed": thread_id}

    @app.get("/runs/{thread_id}", dependencies=[Depends(require_tier("observe"))])
    def run_status(thread_id: str):
        run = runtime.snapshot(thread_id)
        if not run:
            raise HTTPException(status_code=404, detail="unknown run")
        return run

    return app
