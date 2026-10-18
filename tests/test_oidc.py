"""OIDC 검증을 가짜 키·JWKS 로 검증 — OIDC 없이."""
import json, time
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from hwp_cowork import oidc as O
from hwp_cowork.auth import OPEN_PATHS, Principal


@pytest.fixture
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def cfg():
    return O.OIDCConfig(issuer="https://auth.example/application/o/nas/", client_id="nas",
                        resource_url="https://nas.example/mcp")


@pytest.fixture
def verifier(cfg, keypair, monkeypatch):
    v = O.OIDCVerifier(cfg)
    pub = keypair.public_key()

    class FakeKey:
        key = pub

    monkeypatch.setattr(v._jwks, "get_signing_key_from_jwt", lambda tok: FakeKey())
    return v


def mint(keypair, **claims):
    base = {"iss": "https://auth.example/application/o/nas/", "aud": "nas", "sub": "alice",
            "exp": int(time.time()) + 300, "preferred_username": "alice"}
    return jwt.encode({**base, **claims}, keypair, algorithm="RS256", headers={"kid": "k1"})


def test_valid_token_maps_to_principal(verifier, keypair):
    reg = O.Registry({"alice": Principal("Alice", "alice@example.com", "all", True)}, verifier)
    p = reg.resolve({"authorization": "Bearer " + mint(keypair)})
    assert p and p.label == "Alice" and p.write


def test_bad_aud_iss_exp_rejected(verifier, keypair):
    reg = O.Registry({"alice": Principal("Alice", "alice@example.com", "all", True)}, verifier)
    assert reg.resolve({"authorization": "Bearer " + mint(keypair, aud="other")}) is None
    assert reg.resolve({"authorization": "Bearer " + mint(keypair, iss="https://evil/")}) is None
    assert reg.resolve({"authorization": "Bearer " + mint(keypair, exp=int(time.time()) - 5)}) is None
    assert reg.resolve({"authorization": "Bearer " + mint(keypair, preferred_username="nobody")}) is None
    assert reg.resolve({"authorization": "Bearer garbage"}) is None


def test_static_token_still_works(verifier):
    tok = "s" * 40
    reg = O.Registry({}, verifier, {tok: Principal("static", "s@x", "all")})
    assert reg.resolve({"Authorization": "Bearer " + tok}).label == "static"
    assert reg.resolve({"Authorization": "Bearer " + "x" * 40}) is None


def test_middleware_metadata_and_401(cfg, verifier, keypair):
    reg = O.Registry({"alice": Principal("Alice", "alice@example.com", "all", True)}, verifier)
    inner = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("in"), methods=["GET"]),
                              Route("/healthz", lambda r: PlainTextResponse("ok"))])
    app = O.AuthMiddleware(inner, reg, cfg, realm="hwp-cowork", open_paths=OPEN_PATHS)
    c = TestClient(app)
    assert c.get("/healthz").text == "ok"
    m = c.get("/.well-known/oauth-protected-resource").json()
    assert m["resource"] == "https://nas.example/mcp" and m["authorization_servers"] == [cfg.base]
    r = c.get("/mcp")
    assert r.status_code == 401
    assert 'resource_metadata="https://nas.example/.well-known/oauth-protected-resource"' in r.headers["www-authenticate"]
    assert c.get("/mcp", headers={"Authorization": "Bearer " + mint(keypair)}).text == "in"


def test_signed_link_paths_bypass_auth(cfg, verifier):
    reg = O.Registry({"alice": Principal("Alice", "e", "all", True)}, verifier)
    inner = Starlette(routes=[Route("/dl/{t}", lambda r: PlainTextResponse("file")),
                              Route("/mcp", lambda r: PlainTextResponse("in"))])
    c = TestClient(O.AuthMiddleware(inner, reg, cfg, realm="hwp-cowork", open_paths=OPEN_PATHS))
    assert c.get("/dl/abc").text == "file"
    assert c.get("/mcp").status_code == 401
