"""TLS between services: the generated certificates really work, and a service refuses a half-configured pair."""

import threading
import time

import httpx
import pytest
import uvicorn
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_layer.server import free_port
from deploylab.certs import make_certs
from deploylab.config import Common, ConfigError, Env
from deploylab.runtime import Lifecycle, make_app


def test_half_configured_tls_is_a_startup_error():
    env = Env({"TLS_CERT_FILE": "/run/secrets/tls_cert"})
    Common(env, "x", 8000)
    with pytest.raises(ConfigError, match="together"):
        env.done()


def test_public_url_follows_the_scheme():
    env = Env({"TLS_CERT_FILE": "a", "TLS_KEY_FILE": "b"})
    assert Common(env, "x", 8000).public_url == "https://127.0.0.1:8000"
    assert Common(Env({}), "x", 8000).public_url == "http://127.0.0.1:8000"


def test_a_client_that_trusts_the_lab_ca_connects_and_one_that_does_not_refuses(tmp_path):
    paths = make_certs(tmp_path)
    lc = Lifecycle("demo", "1")

    async def hello(_):
        return JSONResponse({"hi": 1})

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(make_app(lc, [Route("/hello", hello)]), host="127.0.0.1", port=port, log_level="error",
                                           ssl_certfile=str(paths["cert"]), ssl_keyfile=str(paths["key"])))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        assert httpx.get(f"https://localhost:{port}/hello", verify=str(paths["ca"])).json() == {"hi": 1}
        assert httpx.get(f"https://127.0.0.1:{port}/hello", verify=str(paths["ca"])).status_code == 200      # SAN covers the IP too
        with pytest.raises(httpx.ConnectError):                      # the system trust store does not know the lab CA
            httpx.get(f"https://localhost:{port}/hello")
        with pytest.raises(httpx.ConnectError):                      # a name the certificate does not cover is refused
            httpx.get(f"https://127.0.0.2:{port}/hello", verify=str(paths["ca"]))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
