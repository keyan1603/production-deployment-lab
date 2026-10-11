"""Create the five secret files compose mounts (secrets/ is gitignored). Values are random, except the model key."""

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploylab.certs import make_certs  # noqa: E402
from deploylab.stack import ROOT, load_gemini_key  # noqa: E402

d = ROOT / "secrets"
d.mkdir(exist_ok=True)
ticket_key, know_key = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
files = {"token_secret": secrets.token_urlsafe(48), "gateway_key_ticket": ticket_key, "gateway_key_knowledge": know_key,
         "gateway_service_keys": f"ticket-agent={ticket_key},knowledge-agent={know_key}", "gemini_key": load_gemini_key()}
for name, value in files.items():
    (d / name).write_text(value)
make_certs(d)                                  # ca.pem, tls_cert.pem, tls_key.pem
# The gateway is the only service that also talks to the public internet, so its trust store is the public roots
# PLUS the lab CA. Every other service trusts the lab CA alone and cannot be talked into trusting anything else.
import certifi  # noqa: E402
public_roots = Path(certifi.where()).read_text(encoding="utf-8")
(d / "ca_bundle.pem").write_text(public_roots + "\n" + (d / "ca.pem").read_text(encoding="utf-8"))
print("wrote", ", ".join(sorted(files) + ["ca.pem", "ca_bundle.pem", "tls_cert.pem", "tls_key.pem"]), "to", d)
