"""The whole system against the provider simulator: do tickets flow, and do the six monitor probes all pass?

    python -u scripts/sim_smoke.py
"""

import json
import shutil
import sys
import warnings
from pathlib import Path

import httpx

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploylab.loadgen import Sim  # noqa: E402
from deploylab.stack import ROOT, Stack  # noqa: E402
from scripts.save_baseline import calibrate  # noqa: E402
from tickets.data import TICKETS  # noqa: E402

shutil.rmtree(ROOT / "run-sim", ignore_errors=True)
sim = Sim(port=8900, rpm=600)
stack = Stack(run_dir=ROOT / "run-sim", gemini_key="sim-key-not-a-real-key", pin_light=True, version="sim", port_base=8300, provider_url=sim.url)
stack.start()
try:
    print("ready:", stack.wait_ready())
    login = lambda u, a="ticket-agent": httpx.post(f"{stack.urls['identity']}/login", json={"username": u, "audience": a}, timeout=10).json()["token"]
    for t in TICKETS[:4] + TICKETS[7:8]:
        r = httpx.post(f"{stack.urls['tickets']}/tickets", json={"id": t.id, "text": t.text}, timeout=120, headers={"Authorization": f"Bearer {login('sam')}"}).json()
        print(f"  {t.id} ({t.kind}) -> {r['action']:9s} model_calls={r['model_calls']} tools={sorted(set(r['tools_used']))} reasons={r['reasons']}")
        print("     reply:", r["reply"][:140].replace("\n", " "))
    print("probes:")
    metrics = calibrate(stack.urls["identity"], stack.urls["gateway"], stack.urls["knowledge"], (stack.secrets_dir / "gateway_key_monitor").read_text().strip(),
                        path=ROOT / "run-sim" / "baseline.json", save=False)
    print("provider saw:", json.dumps({k: v for k, v in sim.stats().items() if k in ("requests_today", "responses", "max_inflight")}))
finally:
    print("stopping:", json.dumps(stack.stop()))
    sim.stop()
