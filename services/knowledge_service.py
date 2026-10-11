"""Knowledge service: the access-controlled knowledge assistant as an A2A server in its own process.

Built from the AI System Design lab's pieces (`KnowledgeAssistant`, `SecureIndex`, the A2A agent and its
auth middleware). What this module adds is the deployment shape:

* model and embedding calls go to the gateway service; this process holds no model API key;
* the index is built in the background after the port opens, and /readyz stays 503 until it is built;
* the public URL in the agent card comes from configuration (PUBLIC_URL), because the address other
  services use is not the address this process binds to once it runs in a container or behind a proxy;
* /healthz and /readyz are public; every other path except the agent card requires a verified token.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from a2a.types import HTTPAuthSecurityScheme, SecurityRequirement, SecurityScheme, StringList
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from google.adk.a2a.utils.agent_to_a2a import to_a2a

from a2a_layer.server import AUDIENCE, AgentAudit, AuthMiddleware, KnowledgeA2AAgent
from deploylab import logs
from deploylab.config import Common, Env
from deploylab.telemetry import telemetry
from deploylab.releases import Registry, Release, ReleaseSet, admin_routes, patched_prompts
from deploylab.remote import RemoteEmbeddingProvider, RemoteGatewayProvider
from deploylab.runtime import init_telemetry, PROBE_PATHS, Lifecycle, add_runtime, serve
from knowledge.assistant import KnowledgeAssistant
from knowledge.corpus import DOCS
from knowledge.index import SecureIndex

log = logging.getLogger("knowledge")


class ProbeAwareAuth(AuthMiddleware):
    """The lab's auth middleware, except that the two probe paths are public (a load balancer has no token)."""

    async def dispatch(self, request, call_next):
        # The release admin API has its own credential (X-Admin-Key), checked by the handler itself.
        if request.url.path in PROBE_PATHS or request.url.path.startswith("/admin/releases"):
            return await call_next(request)
        return await super().dispatch(request, call_next)


class Settings:
    def __init__(self, env: Env) -> None:
        self.common = Common(env, "knowledge", 8103)
        self.token_secret = env.secret("TOKEN_SECRET", min_length=32)
        self.gateway_url = env.str("GATEWAY_URL", required=True)
        self.gateway_key = env.secret("GATEWAY_SERVICE_KEY", min_length=16)
        self.audit_path = env.str("AUDIT_PATH", "")
        self.releases_dir = Path(env.str("RELEASES_DIR", str(Path(__file__).resolve().parent.parent / "releases")))
        self.active_release = env.str("ACTIVE_RELEASE", "r1-baseline")
        self.admin_key = env.secret("ADMIN_KEY", required=False, min_length=16)
        env.done()


class ReleasingAssistant:
    """What the A2A agent calls. It forwards to whichever knowledge release is active and records the outcome."""

    def __init__(self, rs: ReleaseSet) -> None:
        self.rs = rs

    def ask(self, user, question, corr=""):
        release_id = self.rs.active
        answer = self.rs.component(release_id).ask(user, question, corr)
        self.rs.record(release_id, "active", answer.action, answer.latency_s)
        tel = telemetry()
        tel.inc("ai.knowledge.answers", action=answer.action, release=release_id, degraded=str(bool(answer.degraded)).lower())
        tel.observe("ai.knowledge.duration", answer.latency_s, action=answer.action)
        return answer


def create_app(settings: Settings, assistant: KnowledgeAssistant = None, index: SecureIndex = None, remote: RemoteGatewayProvider = None):
    common = settings.common
    lc = Lifecycle(common.service, common.version)
    rs = ReleaseSet(common.service)
    build = None
    if assistant is None:
        remote = RemoteGatewayProvider(settings.gateway_url, "knowledge-agent", settings.gateway_key)
        index = SecureIndex(RemoteEmbeddingProvider(settings.gateway_url, "knowledge-agent", settings.gateway_key))

        def build(release: Release) -> KnowledgeAssistant:
            with patched_prompts(release.prompts):               # the assistant captures its prompt when it is built
                return KnowledgeAssistant(remote, index)

        registry = Registry(settings.releases_dir)
        initial = registry.get(settings.active_release)
        rs.add(initial, build(initial), make_active=True)
    else:
        rs.add(Release(id="test", prompts={}, models={}), assistant, make_active=True)
    assistant = ReleasingAssistant(rs)
    audit = AgentAudit(path=settings.audit_path or None)
    agent = KnowledgeA2AAgent(
        name="knowledge_agent",
        description="Answers internal policy and runbook questions with citations, limited to what the verified caller may read.",
        assistant=assistant, audit=audit)
    scheme = SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(
        scheme="bearer", bearer_format="signed-token", description="Short-lived, audience-bound token from the identity provider."))
    card = asyncio.run(AgentCardBuilder(agent=agent, rpc_url=common.public_url, security_schemes={"bearer": scheme}).build())
    card.security_requirements.append(SecurityRequirement(schemes={"bearer": StringList(list=[])}))
    app = to_a2a(agent, host=common.host, port=common.port, agent_card=card)
    app.add_middleware(ProbeAwareAuth, secret=settings.token_secret.reveal().encode(), audience=AUDIENCE, audit=audit, service="knowledge-agent")
    add_runtime(app, lc)
    if build is not None and settings.admin_key.reveal():
        app.router.routes[:0] = admin_routes(rs, registry, build, settings.admin_key.reveal())

    if index is not None:
        lc.run_in_background("index", lambda: log.info("index built: %s chunks", index.ingest(DOCS)))
    if remote is not None:
        lc.checks["gateway"] = remote.is_ready
    return app, lc, rs


def main() -> None:
    try:
        settings = Settings(Env())
    except Exception as exc:  # ConfigError
        logs.setup("knowledge")
        log.error("%s", exc)
        raise SystemExit(2)
    logs.setup(settings.common.service, settings.common.log_level)
    init_telemetry(settings.common)
    app, lc, _ = create_app(settings)
    serve(app, lc, settings.common)


if __name__ == "__main__":
    main()
