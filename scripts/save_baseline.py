"""Save the drift baseline for a running system, once, deliberately.

    python scripts/save_baseline.py --identity URL --gateway URL --knowledge URL --gateway-key-file PATH --yes

Runs one full rotation of the monitor's probes against the system and, only if every probe passed and no restricted
fact leaked, writes baselines/production_baseline.json. A person reviews the printed metrics and passes --yes; the
monitor service never writes this file, because a regression saved automatically becomes the new normal.
"""

import argparse
import asyncio
import json
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from a2a_layer.client import KnowledgeClient  # noqa: E402
from deploylab.gate import normalize  # noqa: E402
from deploylab.monitor import PROBE_IDS, Monitor  # noqa: E402
from deploylab.remote import RemoteIdentity  # noqa: E402
from monitoring.drift import save_baseline  # noqa: E402

DEFAULT = pathlib.Path(__file__).resolve().parent.parent / "baselines" / "production_baseline.json"


def calibrate(identity_url: str, gateway_url: str, knowledge_url: str, gateway_key: str, path=DEFAULT, save: bool = False, log=print) -> dict:
    identity, client = RemoteIdentity(identity_url), KnowledgeClient(knowledge_url)

    def ask(case):
        return normalize(asyncio.run(client.ask(case.question, identity.login(case.user, "knowledge-agent"), f"baseline-{case.id}", subject=case.user)))

    def status():
        r = httpx.get(f"{gateway_url}/v1/status", headers={"X-Service": "monitor-agent", "X-Service-Key": gateway_key}, timeout=10)
        r.raise_for_status()
        return r.json()

    mon = Monitor(ask, status, path, min_results=len(PROBE_IDS))
    for _ in PROBE_IDS:
        res = mon.probe_once()
        log(f"  probe {res.case_id} ({res.expect}) -> {'pass' if res.ok else 'FAIL'}{'  LEAK' if res.leaked else ''} {res.error}")
    metrics = mon.drift_metrics()
    log("  metrics:", json.dumps(metrics))
    if metrics["correct_rate"] < 1.0 or metrics["leaks"]:
        raise SystemExit("not saving: a baseline must come from a run where every probe passed and nothing leaked")
    if save:
        save_baseline(metrics, path)
        log("  saved", path)
    return metrics


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity", required=True)
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--knowledge", required=True)
    ap.add_argument("--gateway-key-file", required=True, help="the monitor-agent service key file")
    ap.add_argument("--yes", action="store_true", help="write the baseline (after you have read the metrics)")
    a = ap.parse_args()
    calibrate(a.identity, a.gateway, a.knowledge, pathlib.Path(a.gateway_key_file).read_text().strip(), save=a.yes)
