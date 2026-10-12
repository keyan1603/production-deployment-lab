"""Invariants on the generated Kubernetes manifests: security, probes, least-privilege secrets, network policy, and the gateway's single-instance rule."""

from pathlib import Path

import pytest
import yaml

from scripts import make_k8s

K8S = Path(__file__).resolve().parent.parent / "deploy" / "k8s"
DOCS = [d for f in sorted(K8S.glob("*.yaml")) for d in yaml.safe_load_all(f.read_text(encoding="utf-8")) if d]
DEPLOYMENTS = {d["metadata"]["name"]: d for d in DOCS if d["kind"] == "Deployment"}
SERVICES = {d["metadata"]["name"]: d for d in DOCS if d["kind"] == "Service"}
APP = ("identity", "gateway", "knowledge", "tickets")


def container(name):
    return DEPLOYMENTS[name]["spec"]["template"]["spec"]["containers"][0]


def test_the_committed_manifests_are_what_the_generator_produces():
    for filename, docs in make_k8s.build().items():
        committed = [d for d in yaml.safe_load_all((K8S / filename).read_text(encoding="utf-8")) if d]
        assert committed == [yaml.safe_load(yaml.safe_dump(d)) for d in docs], filename


def test_the_namespace_enforces_the_restricted_pod_security_standard():
    ns = next(d for d in DOCS if d["kind"] == "Namespace")
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


@pytest.mark.parametrize("name", list(DEPLOYMENTS))
def test_every_container_is_locked_down(name):
    c, pod = container(name), DEPLOYMENTS[name]["spec"]["template"]["spec"]
    sc = c["securityContext"]
    assert sc["runAsNonRoot"] and sc["runAsUser"] == 10001 and sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"]
    assert sc["capabilities"]["drop"] == ["ALL"] and sc["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["automountServiceAccountToken"] is False                    # no pod needs to talk to the Kubernetes API
    assert "privileged" not in sc and not pod.get("hostNetwork") and not pod.get("hostPID")


@pytest.mark.parametrize("name", list(DEPLOYMENTS))
def test_every_container_has_all_three_probes_and_resource_bounds(name):
    c = container(name)
    for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
        assert probe in c, (name, probe)
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"            # liveness never looks at dependencies
    assert c["readinessProbe"]["httpGet"]["path"] in ("/readyz", "/healthz")
    assert c["resources"]["requests"]["memory"] and c["resources"]["limits"]["memory"]


@pytest.mark.parametrize("name", APP)
def test_the_app_services_speak_tls_and_the_grace_period_covers_the_drain(name):
    env = {e["name"]: e["value"] for e in container(name)["env"]}
    assert env["TLS_CERT_FILE"].startswith("/run/secrets/") and container(name)["livenessProbe"]["httpGet"]["scheme"] == "HTTPS"
    pod = DEPLOYMENTS[name]["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] >= float(env["DRAIN_SECONDS"]) + float(env["GRACE_SECONDS"])


def test_no_manifest_carries_a_secret_value():
    for d in DEPLOYMENTS.values():
        for e in container(d["metadata"]["name"])["env"]:
            assert not (e["name"].endswith(("SECRET", "_KEY", "_KEYS")) and not e["name"].endswith("_FILE")), e["name"]
            assert "valueFrom" not in e or "secretKeyRef" not in e["valueFrom"]
    assert not any(d["kind"] == "Secret" for d in DOCS), "secrets are created at deploy time, never committed"


def test_each_pod_sees_only_the_secret_files_it_needs():
    for name in APP:
        pod = DEPLOYMENTS[name]["spec"]["template"]["spec"]
        items = {i["key"] for v in pod["volumes"] if "secret" in v for i in v["secret"]["items"]}
        assert items == set(make_k8s.SECRETS[name]), name
    holders = [n for n in APP if "gemini_key" in make_k8s.SECRETS[n]]
    assert holders == ["gateway"]                                          # the model key lives in exactly one pod
    assert "ca_bundle" in make_k8s.SECRETS["gateway"] and "ca_bundle" not in make_k8s.SECRETS["tickets"]
    assert {v["name"] for v in DEPLOYMENTS["provider-sim"]["spec"]["template"]["spec"]["volumes"]} == {"tmp"}      # the simulator mounts no secrets


def test_the_gateway_is_a_single_instance_that_is_replaced_not_overlapped():
    g = DEPLOYMENTS["gateway"]["spec"]
    assert g["replicas"] == 1 and g["strategy"]["type"] == "Recreate"      # two limiters against one provider quota made things worse
    assert "ai-lab/why-one-replica" in DEPLOYMENTS["gateway"]["metadata"]["annotations"]


def test_the_ticket_service_is_redundant_and_rolls_without_losing_capacity():
    t = DEPLOYMENTS["tickets"]["spec"]
    assert t["replicas"] >= 2 and t["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    pdb = next(d for d in DOCS if d["kind"] == "PodDisruptionBudget")
    assert pdb["spec"]["minAvailable"] >= 1 and pdb["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] == "tickets"
    assert t["template"]["spec"]["topologySpreadConstraints"][0]["topologyKey"] == "kubernetes.io/hostname"


def test_services_match_their_containers_and_only_the_front_doors_are_exposed():
    for name, svc in SERVICES.items():
        port = svc["spec"]["ports"][0]
        assert port["targetPort"] == container(name)["ports"][0]["containerPort"] == make_k8s.PORTS[name]
    exposed = {n for n, s in SERVICES.items() if s["spec"].get("type") == "NodePort"}
    assert exposed == {"identity", "tickets"}


def test_the_network_is_default_deny_and_each_service_admits_only_its_callers():
    policies = {d["metadata"]["name"]: d for d in DOCS if d["kind"] == "NetworkPolicy"}
    assert policies["default-deny-ingress"]["spec"]["podSelector"] == {} and "ingress" not in policies["default-deny-ingress"]["spec"]

    def callers(name):
        rule = policies[f"allow-{name}"]["spec"]["ingress"][0]
        return {p["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for p in rule.get("from", [])}

    assert callers("gateway") == {"tickets", "knowledge"} and callers("knowledge") == {"tickets"} and callers("provider-sim") == {"gateway"}
    assert "from" not in policies["allow-tickets"]["spec"]["ingress"][0] and "from" not in policies["allow-identity"]["spec"]["ingress"][0]     # the front doors


def test_the_simulator_is_labelled_as_one():
    assert "ai-lab/simulated" in DEPLOYMENTS["provider-sim"]["metadata"]["annotations"]
    env = {e["name"]: e["value"] for e in container("gateway")["env"]}
    assert env["GOOGLE_GEMINI_BASE_URL"].startswith("http://provider-sim")
