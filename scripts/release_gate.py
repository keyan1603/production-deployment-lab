"""Run a candidate release through the gate.

    python -u scripts/release_gate.py r1-baseline --store          # evaluate the baseline and keep its results
    python -u scripts/release_gate.py r1-baseline --repeat         # baseline against itself: the noise floor
    python -u scripts/release_gate.py r2-shorter-reply             # a candidate against the stored baseline

Exit code: 0 pass (possibly with warnings), 1 the evaluation found a failure, 2 an earlier stage stopped the release.
"""

import warnings
warnings.filterwarnings("ignore")        # ADK marks its A2A support experimental; that is noted in the README, not on every line
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import httpx
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploylab.gate import check_models, compare, evaluate, metrics  # noqa: E402
from deploylab.releases import Registry, ReleaseError, lint  # noqa: E402
from deploylab.stack import ROOT, Stack, load_gemini_key  # noqa: E402

RESULTS = ROOT / "releases" / "results"


def stage(n, name):
    print(f"\n--- stage {n}: {name}")


def stop_with(code, message):
    print(f"\nGATE: {message}")
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("release")
    ap.add_argument("--baseline", default="r1-baseline")
    ap.add_argument("--store", action="store_true", help="evaluate and store results without comparing")
    ap.add_argument("--repeat", action="store_true", help="evaluate again and compare with the stored results of the same release")
    ap.add_argument("--only", default="", help="comma list of ticket/case ids (for a quick look, never for a real gate)")
    args = ap.parse_args()

    registry = Registry(ROOT / "releases")
    key = load_gemini_key()

    stage(1, "lint")
    try:
        release = registry.get(args.release)
    except ReleaseError as exc:
        stop_with(2, f"STOP at lint: {exc}")
    problems = lint(release)
    print("  manifest:", release.id, "digest", release.digest, "parent", release.parent)
    if problems:
        stop_with(2, "STOP at lint: " + "; ".join(problems))
    print("  well formed")

    stage(2, "model pins exist at the provider")
    checked = check_models(release.models, key)
    for pin, (ok, detail) in checked.items():
        print(f"  {pin}: {release.models[pin]} -> {detail}")
    if not all(ok for ok, _ in checked.values()):
        stop_with(2, "STOP at model pins: " + "; ".join(f"{p}={release.models[p]} ({d})" for p, (ok, d) in checked.items() if not ok))

    stage(3, "staging copy of the whole system running this release")
    run_dir = ROOT / "run-gate" / (args.release + ("-repeat" if args.repeat else ""))
    shutil.rmtree(run_dir, ignore_errors=True)
    stack = Stack(run_dir=run_dir, gemini_key=key, pin_light=True, release=release.id, models=release.models, port_base=8200, version=release.digest)
    stack.start()
    try:
        print("  ready after:", stack.wait_ready())
        gw_key = (stack.secrets_dir / "gateway_key_ticket").read_text()
        status = lambda: httpx.get(f"{stack.urls['gateway']}/v1/status", headers={"X-Service": "ticket-agent", "X-Service-Key": gw_key}, timeout=10).json()
        pinned = {r["name"]: r["model"] for r in status()["routes"]}
        print("  gateway runs:", {k: v for k, v in pinned.items() if k.startswith("gemini")})
        want = {"gemini-light": release.models["light"], "gemini-heavy": release.models["heavy"]}
        if any(pinned.get(k) != v for k, v in want.items()):
            stop_with(2, f"STOP at staging: the gateway is running {pinned}, the release pins {want}")
        login = lambda user, aud: httpx.post(f"{stack.urls['identity']}/login", json={"username": user, "audience": aud}, timeout=10).json()["token"]
        served = httpx.get(f"{stack.urls['tickets']}/admin/releases", headers={"X-Admin-Key": stack.admin_key}, timeout=10).json()
        print("  tickets service active release:", served["active"], "| knowledge:", httpx.get(f"{stack.urls['knowledge']}/admin/releases", headers={"X-Admin-Key": stack.admin_key}, timeout=10).json()["active"])

        stage(4, "evaluation (9 tickets, 13 questions, black box)")
        before = status()["audit"]
        t0 = time.time()
        result = evaluate(stack.urls, login, release.id, only=[x for x in args.only.split(",") if x] or None)
        after = status()["audit"]
        result["usage"] = {"tokens": after.get("input_tokens", 0) + after.get("output_tokens", 0) - before.get("input_tokens", 0) - before.get("output_tokens", 0),
                           "cost_usd": round(after.get("total_cost_usd", 0) - before.get("total_cost_usd", 0), 6),
                           "model_calls": after.get("requests", 0) - before.get("requests", 0), "seconds": round(time.time() - t0)}
        result["digest"] = release.digest
    finally:
        stack.stop()

    m = metrics(result)
    print("\n  metrics:", json.dumps({k: v for k, v in m.items() if k != "usage"}))
    print("  usage:", m["usage"])
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{release.id}{'.repeat' if args.repeat else ''}.json"

    if args.store:
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        stop_with(0, f"stored {out.name}")

    stage(5, "comparison with the stored baseline")
    base_path = RESULTS / f"{release.id if args.repeat else args.baseline}.json"
    if not base_path.exists():
        stop_with(2, f"no stored results at {base_path.name}; run the baseline with --store first")
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    baseline = json.loads(base_path.read_text(encoding="utf-8"))
    print(f"  baseline: {baseline['release']} (digest {baseline.get('digest')}) | candidate: {release.id} (digest {release.digest})")
    v = compare(baseline, result)
    b = metrics(baseline)
    print(tabulate([[k, b[k], m[k]] for k in ("ticket_action_ok", "ticket_category_ok", "ticket_severity_ok", "ticket_facts_mean", "ticket_held_unexpectedly",
                                              "injection_stopped", "case_correct", "leaks")],
                   headers=["metric", "baseline", "candidate"], tablefmt="github"))
    for label, items in (("HARD FAILURES", v.hard), ("REGRESSIONS", v.regressions), ("WARNINGS", v.warnings), ("CASE-BY-CASE CHANGES", v.flips)):
        if items:
            print(f"  {label}:")
            for item in items:
                print("    -", item)
    print(f"\nGATE VERDICT for {release.id}: {v.outcome}")
    sys.exit(1 if v.outcome == "FAIL" else 0)


if __name__ == "__main__":
    main()
