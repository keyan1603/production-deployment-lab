"""Structural checks on deploy/compose.yaml and deploy/Dockerfile.

These do NOT build or run anything (no container runtime was available when the files were written). They
prove the declarations are consistent with the code: every service's configuration loads from exactly the
environment and secret files compose declares, only the gateway holds the model key, and the stop grace period
covers the drain and grace the services are configured with.
"""

from pathlib import Path

import pytest
import yaml

from deploylab.config import Env
from services import gateway_service, identity_service, knowledge_service, ticket_service

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
COMPOSE = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]


def resolved_env(name: str, tmp_path: Path) -> dict:
    """The service's environment as the container would see it, with each mounted secret as a real file."""
    svc = SERVICES[name]
    env = {k: str(v) for k, v in svc["environment"].items()}
    for key, value in list(env.items()):
        if key.endswith("_FILE"):
            secret_name = Path(value).name
            assert secret_name in svc["secrets"], f"{name}: {key} points at /run/secrets/{secret_name}, which is not mounted"
            f = tmp_path / f"{name}_{secret_name}"
            f.write_text(("k" * 8 + secret_name + "x" * 40)[:48])
            env[key] = str(f)
    return env


LOADERS = {
    "identity": lambda e: identity_service.build(Env(e)),
    "gateway": lambda e: gateway_service.Settings(Env({**e, "GATEWAY_SERVICE_KEYS": "ticket-agent=" + "t" * 20})
                                                  if "GATEWAY_SERVICE_KEYS_FILE" not in e else Env(e)),
    "knowledge": lambda e: knowledge_service.Settings(Env(e)),
    "tickets": lambda e: ticket_service.Settings(Env(e)),
}


@pytest.mark.parametrize("name", list(SERVICES))
def test_each_service_loads_its_configuration_from_what_compose_declares(name, tmp_path):
    if name == "gateway":
        env = resolved_env(name, tmp_path)
        # the service-keys file must be in name=key form; write a realistic one
        keys = Path(env["GATEWAY_SERVICE_KEYS_FILE"])
        keys.write_text("ticket-agent=" + "t" * 24 + ",knowledge-agent=" + "u" * 24)
        gateway_service.Settings(Env(env))
    else:
        LOADERS[name](resolved_env(name, tmp_path))


def test_only_the_gateway_is_given_the_model_provider_key():
    holders = [n for n, s in SERVICES.items() if "gemini_key" in s.get("secrets", [])]
    assert holders == ["gateway"]
    for n, s in SERVICES.items():
        assert not any("GEMINI" in k for k in s["environment"] if n != "gateway")


def test_no_secret_value_is_written_into_the_compose_file():
    text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "AIza" not in text and "Bearer " not in text
    for s in SERVICES.values():
        for k in s["environment"]:
            assert not k.endswith(("SECRET", "_KEY", "_KEYS")), f"{k} would carry a secret value in the environment"


def test_stop_grace_covers_drain_plus_grace():
    for name, s in SERVICES.items():
        stop = int(str(s["stop_grace_period"]).rstrip("s"))
        env = s["environment"]
        assert stop >= float(env["DRAIN_SECONDS"]) + float(env["GRACE_SECONDS"]), name


def test_containers_are_unprivileged_and_read_only():
    for name, s in SERVICES.items():
        assert s["read_only"] is True and s["user"] == "10001" and s["cap_drop"] == ["ALL"], name
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001" in dockerfile and "HEALTHCHECK" in dockerfile


def test_services_find_each_other_by_compose_service_name_and_port():
    ports = {n: s["environment"]["PORT"] for n, s in SERVICES.items()}
    wanted = {"GATEWAY_URL": "gateway", "KNOWLEDGE_URL": "knowledge", "IDENTITY_URL": "identity"}
    for s in SERVICES.values():
        for var, target in wanted.items():
            if var in s["environment"]:
                assert s["environment"][var] == f"http://{target}:{ports[target]}"
    assert SERVICES["knowledge"]["environment"]["PUBLIC_URL"] == f"http://knowledge:{ports['knowledge']}"
