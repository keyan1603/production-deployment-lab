"""Generate the Prometheus rule files from observability/slo.json.

    python scripts/make_slo_rules.py            # writes observability/prometheus/rules/slo.rules.yml (standard windows)
                                                # and slo.lab.rules.yml (minutes instead of hours, for a short live run)
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from deploylab import slo  # noqa: E402

root = pathlib.Path(__file__).resolve().parent.parent / "observability"
slos = slo.load(root / "slo.json")
out = root / "prometheus" / "rules"
out.mkdir(parents=True, exist_ok=True)
for name, windows in (("slo.rules.yml", slo.STANDARD), ("slo.lab.rules.yml", slo.LAB)):
    (out / name).write_text(slo.dump(slo.build_rules(slos, windows)), encoding="utf-8")
    print("wrote", out / name)
