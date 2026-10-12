"""Create the secret files for the cluster (secrets-k8s/, gitignored) and print the kubectl command that loads them.

The model key in this Secret is a placeholder: the cluster talks to the provider simulator, and a real API key must not be put in a
lab cluster. Everything else is random, generated here, and never written to a manifest.
"""

import pathlib
import secrets
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from deploylab.certs import make_certs  # noqa: E402

try:
    import certifi
except ImportError:                                        # pragma: no cover
    certifi = None


def make(directory: pathlib.Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    ticket_key, know_key, mon_key = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    files = {"token_secret": secrets.token_urlsafe(48), "admin_key": secrets.token_urlsafe(24), "gateway_key_ticket": ticket_key,
             "gateway_key_knowledge": know_key, "gateway_key_monitor": mon_key,
             "gateway_service_keys": f"ticket-agent={ticket_key},knowledge-agent={know_key},monitor-agent={mon_key}",
             "gemini_key": "sim-key-not-a-real-key"}
    for name, value in files.items():
        (directory / name).write_text(value)
    make_certs(directory)                                   # ca.pem, tls_cert.pem, tls_key.pem
    bundle = (pathlib.Path(certifi.where()).read_text(encoding="utf-8") if certifi else "") + "\n" + (directory / "ca.pem").read_text(encoding="utf-8")
    (directory / "ca_bundle.pem").write_text(bundle)
    keys = {**{k: k for k in files}, "tls_cert": "tls_cert.pem", "tls_key": "tls_key.pem", "ca_pem": "ca.pem", "ca_bundle": "ca_bundle.pem"}
    return {key: directory / filename for key, filename in keys.items()}


def apply(directory: pathlib.Path, context: str = "kind-ai-lab", namespace: str = "ai-lab") -> None:
    """(Re)create the Secret from the files. Called with a list of arguments, so no shell touches the paths."""
    import subprocess
    mapping = make(directory)
    subprocess.run(["kubectl", "--context", context, "-n", namespace, "delete", "secret", "ai-secrets", "--ignore-not-found"], check=True)
    subprocess.run(["kubectl", "--context", context, "-n", namespace, "create", "secret", "generic", "ai-secrets",
                    *[f"--from-file={k}={v}" for k, v in mapping.items()]], check=True)


if __name__ == "__main__":
    apply(pathlib.Path(__file__).resolve().parent.parent / "secrets-k8s")
