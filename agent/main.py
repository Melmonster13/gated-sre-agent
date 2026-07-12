"""Process entrypoint (DESIGN §1): watcher + graph + API in one process.

Usage:
    python -m agent.main
"""

import logging
import sqlite3
import threading

import uvicorn
from kubernetes import client, config as kube_config
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.api import build_app
from agent.config import load_config
from agent.graph import build_graph
from agent.llm import diagnose
from agent.runtime import Runtime
from agent.tools import LiveActuator, LiveCluster
from agent.watcher import Watcher, make_condition_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main():
    cfg = load_config()
    try:
        kube_config.load_incluster_config()   # deployed as a pod (DESIGN §1)
    except kube_config.ConfigException:
        kube_config.load_kube_config()        # dev: local kubeconfig
    core, apps = client.CoreV1Api(), client.AppsV1Api()

    checkpointer = SqliteSaver(sqlite3.connect(cfg.db_path, check_same_thread=False))
    graph = build_graph(
        cfg,
        evidence_source=LiveCluster(core, apps),
        actuator=LiveActuator(core),
        condition_check=make_condition_check(core),
        diagnose_fn=diagnose,
        checkpointer=checkpointer,
    )
    runtime = Runtime(graph)
    watcher = Watcher(core, cfg, runtime.start_run, runtime.has_active_run)
    threading.Thread(target=watcher.run, daemon=True).start()

    uvicorn.run(build_app(runtime, cfg), host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
