"""OIDC 리소스 서버 인증 (Authentik·Keycloak·Zitadel 등 OIDC 제공자면 된다).

흐름 (MCP 사양, RFC 9728 + OIDC):
  1. Claude 가 `/.well-known/oauth-protected-resource` 를 읽어 인증 서버(issuer)를 안다
  2. 제공자에 로그인 → access token(JWT, RS256) 을 Authorization: Bearer 로 보낸다
  3. 여기서 서명(JWKS)·iss·aud(client_id)·exp 를 검증하고 preferred_username 을 꺼낸다
  4. principals 파일(username → 라벨·권한)에서 Principal 을 찾는다. 없는 사용자는 401

토큰에 preferred_username 이 없으면 userinfo 로 한 번 묻고 토큰 해시로 캐시한다.
정적 Bearer 토큰(TOKENS_FILE)도 같이 받을 수 있다 — 비우면 OIDC 만.

환경변수 (접두사 HWPCOWORK_):
  <P>_OIDC_ISSUER       https://auth.example.com/application/o/<slug>/
  <P>_OIDC_CLIENT_ID    제공자 쪽 public client 의 client_id (= aud)
  <P>_RESOURCE_URL      https://<host>/mcp  (protected resource metadata 의 resource)
  <P>_PRINCIPALS_FILE   [{"username": "alice", "label": "Alice", ...권한}]
  <P>_OIDC_DCR          기본 1. 0 이면 자동 등록을 끄고 제공자를 직접 인증 서버로 알린다

자동 등록 (DCR, RFC 7591): DCR 이 없는 제공자는 커넥터에 client_id 를 손으로 넣어야 한다.
그래서 이 서버가 인증 서버 메타데이터(RFC 8414)를 대신 낸다 — authorize·token 은 제공자 그대로,
registration_endpoint 만 여기 `/register`. `/register` 는 늘 같은 client_id 를 **secret 없이** 돌려준다.
제공자 쪽 클라이언트는 public(PKCE) 이어야 한다. secret 은 어디에도 내보내지 않는다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
import jwt
from jwt import PyJWKClient
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("oidc")

WELL_KNOWN = "/.well-known/oauth-protected-resource"
AS_WELL_KNOWN = ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration")
REGISTER = "/register"
_UPSTREAM_KEYS = ("authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint",
                  "revocation_endpoint", "scopes_supported", "response_types_supported",
                  "grant_types_supported", "code_challenge_methods_supported")


class AuthConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    resource_url: str
    jwks_url: str = ""
    userinfo_url: str = ""
    dcr: bool = True
    scopes: tuple[str, ...] = ("openid", "profile", "email", "offline_access")

    def __post_init__(self) -> None:
        iss = self.issuer if self.issuer.endswith("/") else self.issuer + "/"
        object.__setattr__(self, "issuer", iss)
        if not self.jwks_url:
            object.__setattr__(self, "jwks_url", iss + "jwks/")
        if not self.userinfo_url:   # Authentik 은 userinfo 가 슬러그 없이 /application/o/userinfo/
            object.__setattr__(self, "userinfo_url", iss.rsplit("/application/o/", 1)[0] + "/application/o/userinfo/")

    @classmethod
    def from_env(cls, env: Mapping[str, str], prefix: str) -> "OIDCConfig | None":
        iss = env.get(f"{prefix}_OIDC_ISSUER", "").strip()
        cid = env.get(f"{prefix}_OIDC_CLIENT_ID", "").strip()
        res = env.get(f"{prefix}_RESOURCE_URL", "").strip()
        if not (iss or cid):
            return None
        if not (iss and cid and res):
            raise AuthConfigError(f"{prefix}_OIDC_ISSUER / {prefix}_OIDC_CLIENT_ID / {prefix}_RESOURCE_URL 이 모두 필요하다.")
        return cls(issuer=iss, client_id=cid, resource_url=res,
                   jwks_url=env.get(f"{prefix}_OIDC_JWKS_URL", "").strip(),
                   userinfo_url=env.get(f"{prefix}_OIDC_USERINFO_URL", "").strip(),
                   dcr=env.get(f"{prefix}_OIDC_DCR", "1").strip() not in ("0", "false", "no"))

    @property
    def base(self) -> str:
        return self.resource_url.rsplit("/mcp", 1)[0].rstrip("/")

    def metadata(self) -> dict[str, Any]:
        # DCR 이면 인증 서버를 이 서버로 알려 Claude 가 여기 메타데이터(registration_endpoint)를 읽게 한다
        auth_server = self.base if self.dcr else self.issuer
        return {"resource": self.resource_url, "authorization_servers": [auth_server],
                "bearer_methods_supported": ["header"], "scopes_supported": list(self.scopes)}


class DCRProxy:
    """제공자 discovery 에서 issuer·registration_endpoint 만 바꿔 내고, /register 에 고정 public 클라이언트를 준다."""

    def __init__(self, cfg: OIDCConfig, ttl: float = 3600) -> None:
        self.cfg, self.ttl = cfg, ttl
        self._cache: tuple[float, dict[str, Any]] | None = None

    async def upstream(self) -> dict[str, Any]:
        now = time.time()
        if self._cache and self._cache[0] > now:
            return self._cache[1]
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(self.cfg.issuer + ".well-known/openid-configuration")
            r.raise_for_status()
            doc = r.json()
        self._cache = (now + self.ttl, doc)
        return doc

    async def as_metadata(self) -> dict[str, Any]:
        up = await self.upstream()
        out: dict[str, Any] = {k: up[k] for k in _UPSTREAM_KEYS if k in up}
        out.update(issuer=self.cfg.base, registration_endpoint=self.cfg.base + REGISTER,
                   token_endpoint_auth_methods_supported=["none"], code_challenge_methods_supported=["S256"])
        return out

    def register(self, req: Mapping[str, Any]) -> dict[str, Any]:
        uris = req.get("redirect_uris")
        return {
            "client_id": self.cfg.client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": uris if isinstance(uris, list) else [],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": str(req.get("client_name") or "Claude")[:100],
        }


class OIDCVerifier:
    """JWT 검증 + username 추출. 스레드 안전. JWKS 는 PyJWKClient 가 캐시한다."""

    def __init__(self, cfg: OIDCConfig) -> None:
        self.cfg = cfg
        self._jwks = PyJWKClient(
            cfg.jwks_url, cache_keys=True, lifespan=3600,
            # 기본 UA(Python-urllib)는 Cloudflare 가 403 으로 막는다
            headers={"User-Agent": "hwp-cowork-oidc/1.0"},
        )
        self._userinfo: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def username(self, token: str) -> tuple[str, dict[str, Any]] | None:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            # iss 는 끝 슬래시 유무가 달라질 수 있어 아래에서 직접 비교한다
            claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=self.cfg.client_id,
                                options={"require": ["exp", "sub"], "verify_iss": False})
        except jwt.PyJWTError as e:
            log.info("JWT 거부: %s", e)
            return None
        iss = str(claims.get("iss", ""))
        if iss.rstrip("/") != self.cfg.issuer.rstrip("/"):
            log.info("JWT iss 불일치: %s", iss)
            return None
        name = claims.get("preferred_username")
        if not name:
            info = self._fetch_userinfo(token)
            name = (info or {}).get("preferred_username")
            if info:
                claims = {**claims, **info}
        if not name:
            log.info("JWT 에 preferred_username 없음 (sub=%s)", claims.get("sub"))
            return None
        return str(name), claims

    def _fetch_userinfo(self, token: str) -> dict[str, Any] | None:
        h = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock:
            hit = self._userinfo.get(h)
            if hit and hit[0] > now:
                return hit[1]
        try:
            r = httpx.get(self.cfg.userinfo_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if r.status_code != 200:
                return None
            info = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        with self._lock:
            if len(self._userinfo) > 256:
                self._userinfo.clear()
            self._userinfo[h] = (now + 300, info)
        return info


class Registry:
    """헤더 → Principal. OIDC(username 매핑) 와 정적 토큰(전환기) 을 함께 본다.

    principals: username → Principal (서버별 dataclass, `label` 필드가 있어야 한다)
    static:     token → Principal (없으면 빈 dict)
    """

    def __init__(self, principals: dict[str, Any], verifier: OIDCVerifier | None,
                 static: dict[str, Any] | None = None) -> None:
        if not principals and not static:
            raise AuthConfigError("주체가 하나도 없다 (PRINCIPALS_FILE 또는 TOKENS_FILE).")
        for t in (static or {}):
            if len(t) < 32:
                raise AuthConfigError("정적 토큰은 32자 이상이어야 한다.")
        self.principals = principals
        self.verifier = verifier
        self.static = static or {}

    @property
    def labels(self) -> list[str]:
        out = [f"{u}→{p.label}" for u, p in self.principals.items()]
        out += [f"static:{p.label}" for p in self.static.values()]
        return out

    def resolve(self, headers: Mapping[str, str] | None) -> Any | None:
        if not headers:
            return None
        raw = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                raw = v
                break
        if not raw.lower().startswith("bearer "):
            return None
        token = raw[7:].strip()
        found = None
        for t, p in self.static.items():          # 전부 비교해 타이밍이 주체 수에만 비례하게
            if hmac.compare_digest(token, t):
                found = p
        if found is not None:
            return found
        if self.verifier is None or token.count(".") != 2:
            return None
        got = self.verifier.username(token)
        if got is None:
            return None
        name, _claims = got
        p = self.principals.get(name)
        if p is None:
            log.warning("OIDC 로그인은 됐으나 principals 에 없는 사용자: %s", name)
        return p


def load_json_list(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise AuthConfigError(f"{path} 는 JSON 배열이어야 한다.")
    return raw


class AuthMiddleware:
    """/healthz 와 protected-resource metadata 는 열고, 나머지는 Bearer 필수.
    401 에 WWW-Authenticate: resource_metadata 를 실어 Claude 가 OAuth 를 시작하게 한다."""

    def __init__(self, app: ASGIApp, registry: Registry, oidc: OIDCConfig | None, realm: str,
                 open_paths: set[str] | None = None, public_base: str = "") -> None:
        self.app, self.registry, self.oidc, self.realm = app, registry, oidc, realm
        self.open = {"/healthz", WELL_KNOWN, WELL_KNOWN + "/mcp"} | (open_paths or set())
        self.public_base = public_base.rstrip("/")
        self.dcr = DCRProxy(oidc) if oidc and oidc.dcr else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in (WELL_KNOWN, WELL_KNOWN + "/mcp") and self.oidc:
            await JSONResponse(self.oidc.metadata())(scope, receive, send)
            return
        if self.dcr and any(path in (p, p + "/mcp") for p in AS_WELL_KNOWN):
            try:
                body = await self.dcr.as_metadata()
            except (httpx.HTTPError, ValueError) as e:
                log.error("OIDC discovery 실패: %s", e)
                await JSONResponse({"error": "upstream_unavailable"}, status_code=502)(scope, receive, send)
                return
            await JSONResponse(body)(scope, receive, send)
            return
        if self.dcr and path == REGISTER:
            await self._register(scope, receive, send)
            return
        if path in self.open or any(path.startswith(p + "/") for p in self.open if p not in ("/healthz",)):
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        who = self.registry.resolve(headers)
        if who is None:
            log.warning("인증 실패: path=%s client=%s", path, scope.get("client"))
            www = f'Bearer realm="{self.realm}"'
            if self.oidc:
                base = self.public_base or self.oidc.resource_url.rsplit("/mcp", 1)[0]
                www += f', resource_metadata="{base}{WELL_KNOWN}"'
            await JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": www})(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _register(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("method") != "POST":
            await JSONResponse({"error": "method_not_allowed"}, status_code=405)(scope, receive, send)
            return
        raw = b""
        while True:
            msg = await receive()
            raw += msg.get("body", b"")
            if len(raw) > 64_000:
                await JSONResponse({"error": "invalid_client_metadata"}, status_code=413)(scope, receive, send)
                return
            if not msg.get("more_body"):
                break
        try:
            req = json.loads(raw or b"{}")
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            await JSONResponse({"error": "invalid_client_metadata"}, status_code=400)(scope, receive, send)
            return
        log.info("DCR 등록: client=%s name=%s", scope.get("client"), req.get("client_name"))
        await JSONResponse(self.dcr.register(req), status_code=201)(scope, receive, send)
