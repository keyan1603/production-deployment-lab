"""Generate the Kubernetes manifests (deploy/k8s/*.yaml) for the services.

    python scripts/make_k8s.py

Generated rather than hand-written so the things that must agree do: the secret files each pod may see (least privilege), the ports
the probes and Services use, the security context every container gets, and the one rule that does not follow the pattern (the gateway is
a single instance with a Recreate strategy, because its rate limiter, breakers and counters live in process memory).
"""

import pathlib

import yaml

OUT = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "k8s"
NS = "ai-lab"
IMAGE = "localhost/production-deployment-lab:phase5"
PORTS = {"identity": 8101, "gateway": 8102, "knowledge": 8103, "tickets": 8104, "provider-sim": 8900}

# Which secret keys each pod may mount (as files under /run/secrets). Nothing else is visible to it.
SECRETS = {
    "identity": ["token_secret", "tls_cert", "tls_key", "ca_pem"],
    "gateway": ["gemini_key", "gateway_service_keys", "tls_cert", "tls_key", "ca_bundle"],
    "knowledge": ["token_secret", "gateway_key_knowledge", "tls_cert", "tls_key", "ca_pem", "admin_key"],
    "tickets": ["token_secret", "gateway_key_ticket", "tls_cert", "tls_key", "ca_pem", "admin_key"],
}
SECRET_FILE = {"tls_cert": "tls_cert.pem", "tls_key": "tls_key.pem", "ca_pem": "ca.pem", "ca_bundle": "ca_bundle.pem"}  # key in the Secret -> file name


def labels(name):
    return {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": "ai-lab"}


def security_context():
    return {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}


def probe_set(port, scheme="HTTPS"):
    http = lambda path: {"httpGet": {"path": path, "port": port, "scheme": scheme}}
    return {
        # startup: give a slow starter (the knowledge index) time without letting liveness kill it
        "startupProbe": {**http("/healthz"), "periodSeconds": 3, "failureThreshold": 40},
        # liveness never looks at dependencies: a dependency outage must not restart a healthy pod
        "livenessProbe": {**http("/healthz"), "periodSeconds": 10, "timeoutSeconds": 3, "failureThreshold": 3},
        # readiness does look at them; failing it takes the pod out of the Service until it recovers
        "readinessProbe": {**http("/readyz"), "periodSeconds": 5, "timeoutSeconds": 3, "failureThreshold": 2},
    }


def secret_volume(name):
    return {"name": "secrets", "secret": {"secretName": "ai-secrets", "defaultMode": 0o440,
                                          "items": [{"key": k, "path": SECRET_FILE.get(k, k)} for k in SECRETS[name]]}}


# DRAIN_SECONDS must outlast the time Kubernetes needs to stop sending traffic to a pod that has started to fail its readiness probe:
# up to periodSeconds x failureThreshold (5 x 2 = 10 s) plus the time the Service's endpoints take to propagate. Measured: 5 s was not enough.
def common_env(name):
    port = PORTS[name]
    env = {"ENVIRONMENT": "kubernetes", "HOST": "0.0.0.0", "PORT": str(port), "DRAIN_SECONDS": "15", "GRACE_SECONDS": "60", "LOG_LEVEL": "INFO",
           "APP_VERSION": "phase5", "TLS_CERT_FILE": "/run/secrets/tls_cert.pem", "TLS_KEY_FILE": "/run/secrets/tls_key.pem",
           "SSL_CERT_FILE": "/run/secrets/ca_bundle.pem" if name == "gateway" else "/run/secrets/ca.pem"}
    return env


def deployment(name, module, env, replicas=1, strategy=None, extra_pod=None, resources=None, secrets=True, plain_http=False):
    port = PORTS[name]
    container = {
        "name": name, "image": IMAGE, "imagePullPolicy": "IfNotPresent", "command": ["python", "-m", module],
        "ports": [{"name": "https" if not plain_http else "http", "containerPort": port}],
        "env": [{"name": k, "value": v} for k, v in env.items()],
        "securityContext": security_context(),
        "resources": resources or {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        **probe_set(port, "HTTP" if plain_http else "HTTPS"),
    }
    volumes = [{"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}]
    if secrets:
        container["volumeMounts"].append({"name": "secrets", "mountPath": "/run/secrets", "readOnly": True})
        volumes.append(secret_volume(name))
    pod = {"metadata": {"labels": labels(name)},
           "spec": {"securityContext": {"runAsNonRoot": True, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                    "automountServiceAccountToken": False,
                    # longer than DRAIN_SECONDS + GRACE_SECONDS, so the kubelet never kills a pod that is still draining
                    "terminationGracePeriodSeconds": 75, "containers": [container], "volumes": volumes, **(extra_pod or {})}}
    spec = {"replicas": replicas, "selector": {"matchLabels": {"app.kubernetes.io/name": name}}, "template": pod}
    if strategy:
        spec["strategy"] = strategy
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NS, "labels": labels(name)}, "spec": spec}


def service(name, node_port=None):
    spec = {"selector": {"app.kubernetes.io/name": name}, "ports": [{"name": "https", "port": PORTS[name], "targetPort": PORTS[name]}]}
    if node_port:
        spec.update({"type": "NodePort"})
        spec["ports"][0]["nodePort"] = node_port
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS, "labels": labels(name)}, "spec": spec}


def netpol(name, allow_from, port):
    peers = [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": p}}} for p in allow_from]
    ingress = [{"from": peers, "ports": [{"port": port, "protocol": "TCP"}]}] if peers else [{"ports": [{"port": port, "protocol": "TCP"}]}]
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": f"allow-{name}", "namespace": NS},
            "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": name}}, "policyTypes": ["Ingress"], "ingress": ingress}}


def build() -> dict:
    spread = {"topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "ScheduleAnyway",
                                             "labelSelector": {"matchLabels": {"app.kubernetes.io/name": "tickets"}}}]}
    files = {}
    files["00-namespace.yaml"] = [{
        "apiVersion": "v1", "kind": "Namespace",
        # The cluster itself refuses any pod that is not non-root, unprivileged, capability-free and seccomp-confined.
        "metadata": {"name": NS, "labels": {"pod-security.kubernetes.io/enforce": "restricted", "pod-security.kubernetes.io/enforce-version": "latest"}}}]

    identity_env = {**common_env("identity"), "TOKEN_SECRET_FILE": "/run/secrets/token_secret"}
    gateway_env = {**common_env("gateway"), "GEMINI_API_KEY_FILE": "/run/secrets/gemini_key", "GATEWAY_SERVICE_KEYS_FILE": "/run/secrets/gateway_service_keys",
                   "REQUESTS_PER_MINUTE": "600", "PIN_LIGHT": "true", "CACHE_SERVICES": "knowledge-agent", "STATE_DIR": "/var/lib/gateway",
                   # SIMULATED PROVIDER: this cluster has no model API key; the simulator answers in the Gemini wire format.
                   "GOOGLE_GEMINI_BASE_URL": f"http://provider-sim:{PORTS['provider-sim']}"}
    knowledge_env = {**common_env("knowledge"), "TOKEN_SECRET_FILE": "/run/secrets/token_secret", "GATEWAY_URL": f"https://gateway:{PORTS['gateway']}",
                     "GATEWAY_SERVICE_KEY_FILE": "/run/secrets/gateway_key_knowledge", "PUBLIC_URL": f"https://knowledge:{PORTS['knowledge']}",
                     "ADMIN_KEY_FILE": "/run/secrets/admin_key", "ACTIVE_RELEASE": "r1-baseline", "RELEASES_DIR": "/app/releases"}
    tickets_env = {**common_env("tickets"), "TOKEN_SECRET_FILE": "/run/secrets/token_secret", "GATEWAY_URL": f"https://gateway:{PORTS['gateway']}",
                   "GATEWAY_SERVICE_KEY_FILE": "/run/secrets/gateway_key_ticket", "KNOWLEDGE_URL": f"https://knowledge:{PORTS['knowledge']}",
                   "IDENTITY_URL": f"https://identity:{PORTS['identity']}", "ADMIN_KEY_FILE": "/run/secrets/admin_key", "ACTIVE_RELEASE": "r1-baseline",
                   "RELEASES_DIR": "/app/releases", "MAX_CONCURRENT_TICKETS": "16", "MAX_WAITING_TICKETS": "64"}

    files["10-identity.yaml"] = [deployment("identity", "services.identity_service", identity_env), service("identity", 30101)]
    gateway = deployment("gateway", "services.gateway_service", gateway_env, replicas=1, strategy={"type": "Recreate"})
    gateway["spec"]["template"]["spec"]["volumes"].append({"name": "state", "emptyDir": {"sizeLimit": "16Mi"}})
    gateway["spec"]["template"]["spec"]["containers"][0]["volumeMounts"].append({"name": "state", "mountPath": "/var/lib/gateway"})
    gateway["metadata"]["annotations"] = {"ai-lab/why-one-replica": "the rate limiter, breakers, cache and quota counters are per process; two instances double the provider quota they ask for"}
    files["11-gateway.yaml"] = [gateway, service("gateway")]
    files["12-knowledge.yaml"] = [deployment("knowledge", "services.knowledge_service", knowledge_env), service("knowledge")]
    files["13-tickets.yaml"] = [
        deployment("tickets", "services.ticket_service", tickets_env, replicas=2, extra_pod=spread,
                   strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}),
        service("tickets", 30104),
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "tickets", "namespace": NS},
         "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "tickets"}}}}]
    sim_env = {"PYTHONUNBUFFERED": "1"}
    sim = deployment("provider-sim", "sim.gemini_sim", sim_env, secrets=False, plain_http=True)
    sim["spec"]["template"]["spec"]["containers"][0]["args"] = ["--host", "0.0.0.0", "--port", str(PORTS["provider-sim"]), "--rpm", "600", "--rpd", "100000"]
    probes = sim["spec"]["template"]["spec"]["containers"][0]
    for key in ("startupProbe", "livenessProbe", "readinessProbe"):
        probes[key]["httpGet"]["path"] = "/healthz"
    sim["metadata"]["annotations"] = {"ai-lab/simulated": "stands in for the model provider's API; not a model"}
    files["14-provider-sim.yaml"] = [sim, {**service("provider-sim"), "spec": {**service("provider-sim")["spec"], "ports": [{"name": "http", "port": PORTS["provider-sim"], "targetPort": PORTS["provider-sim"]}]}}]

    deny = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "default-deny-ingress", "namespace": NS},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"]}}
    files["20-networkpolicy.yaml"] = [
        deny,
        netpol("identity", [], PORTS["identity"]),                                     # the login endpoint is the front door
        netpol("tickets", [], PORTS["tickets"]),                                        # so is the ticket API
        netpol("gateway", ["tickets", "knowledge"], PORTS["gateway"]),                  # only the services that call models
        netpol("knowledge", ["tickets"], PORTS["knowledge"]),
        netpol("provider-sim", ["gateway"], PORTS["provider-sim"]),
    ]
    return files


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, docs in build().items():
        (OUT / name).write_text("# Generated by scripts/make_k8s.py. Do not edit by hand.\n" + yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
        print("wrote", OUT / name)
