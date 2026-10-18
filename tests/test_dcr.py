"""자동 등록(DCR) 프록시 — OIDC discovery 는 가짜로 대신한다."""
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.testclient import TestClient

from hwp_cowork import oidc as O

UP = {"issuer": "https://auth.example/application/o/x/",
      "authorization_endpoint": "https://auth.example/application/o/authorize/",
      "token_endpoint": "https://auth.example/application/o/token/",
      "jwks_uri": "https://auth.example/application/o/x/jwks/",
      "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"]}


def client(dcr=True):
    cfg = O.OIDCConfig(issuer="https://auth.example/application/o/x/", client_id="x",
                       resource_url="https://x.example/mcp", dcr=dcr)
    reg = O.Registry({}, None, {"s" * 40: SimpleNamespace(label="s")})
    app = O.AuthMiddleware(Starlette(), reg, cfg, realm="x")
    if app.dcr:
        async def fake():
            return UP
        app.dcr.upstream = fake
    return TestClient(app), cfg


def test_resource_metadata_points_to_self():
    c, _ = client()
    assert c.get(O.WELL_KNOWN).json()["authorization_servers"] == ["https://x.example"]


def test_as_metadata_proxies_authentik_without_secret_methods():
    c, _ = client()
    for p in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration",
              "/.well-known/oauth-authorization-server/mcp"):
        m = c.get(p).json()
        assert m["issuer"] == "https://x.example"
        assert m["registration_endpoint"] == "https://x.example/register"
        assert m["authorization_endpoint"] == UP["authorization_endpoint"]
        assert m["token_endpoint"] == UP["token_endpoint"]
        assert m["token_endpoint_auth_methods_supported"] == ["none"]
        assert m["code_challenge_methods_supported"] == ["S256"]


def test_register_returns_public_client_no_secret():
    c, _ = client()
    r = c.post("/register", json={"client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]})
    assert r.status_code == 201
    b = r.json()
    assert b["client_id"] == "x" and "client_secret" not in b
    assert b["token_endpoint_auth_method"] == "none"
    assert c.get("/register").status_code == 405
    assert c.post("/register", content=b"[1]").status_code == 400


def test_dcr_off_keeps_old_behaviour():
    c, cfg = client(dcr=False)
    assert c.get(O.WELL_KNOWN).json()["authorization_servers"] == [cfg.issuer]
    assert c.post("/register", json={}).status_code == 401
    assert c.get("/.well-known/oauth-authorization-server").status_code == 401
