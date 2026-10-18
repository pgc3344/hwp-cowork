"""인증 — OIDC (username → Principal). 사람별 권한은 principals.json 에 둔다. 형식:
  [{"username": "alice", "label": "Alice", "roots": ["docs", "shared"], "write": true}, ...]
  write: 폴더에 사본으로 저장(hwp_save_copy)할 수 있는가. 샌드박스 편집은 누구나 된다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .oidc import AuthConfigError, AuthMiddleware, OIDCConfig, OIDCVerifier, Registry, load_json_list

__all__ = ["AuthConfigError", "AuthMiddleware", "OIDCConfig", "Principal", "LOCAL", "Registry", "build_registry", "OPEN_PATHS"]

OPEN_PATHS = {p.rstrip("/") for p in ("/dl/", "/hu/", "/live/", "/static/", "/studio/", "/sso/", "/internal/")}  # /internal 은 터널 경유를 스스로 막는다   # 짧은 링크는 스스로 인증한다


@dataclass(frozen=True)
class Principal:
    label: str
    email: str
    roots: str | frozenset[str]
    write: bool = False
    username: str = "local"

    def can_see(self, root: str) -> bool:
        return self.roots == "all" or root in self.roots

    def visible_roots(self, all_roots: list[str]) -> list[str]:
        return [r for r in all_roots if self.can_see(r)]


LOCAL = Principal(label="local", email="hwp-cowork@localhost", roots="all", write=True, username="local")


def build_registry(known_roots: list[str], env=os.environ, mode: str = "oidc") -> tuple[Registry | None, OIDCConfig | None]:
    """mode: none → None (누구나 LOCAL) · token → 정적 Bearer 토큰 · oidc → OIDC + principals."""
    if mode == "none":
        return None, None
    if mode == "token":
        static: dict[str, Principal] = {}
        if env.get("HWPCOWORK_TOKEN"):
            static[env["HWPCOWORK_TOKEN"].strip()] = LOCAL
        for e in load_json_list(env.get("HWPCOWORK_TOKENS_FILE") or None):
            if "token" not in e or "label" not in e:
                raise AuthConfigError(f"tokens 항목에 token·label 이 필요하다: {e!r}")
            roots = e.get("roots", "all")
            if isinstance(roots, list):
                roots = frozenset(str(r).strip() for r in roots) & frozenset(known_roots)
            u = str(e.get("username") or e["label"]).strip()
            static[str(e["token"])] = Principal(label=str(e["label"]), email=f"{u}@hwp-cowork.local", roots=roots,
                                                write=bool(e.get("write", False)), username=u)
        if not static:
            raise AuthConfigError("token 모드에는 HWPCOWORK_TOKEN 또는 HWPCOWORK_TOKENS_FILE 이 필요하다.")
        return Registry({}, None, static), None
    oidc = OIDCConfig.from_env(env, "HWPCOWORK")
    if oidc is None:
        raise AuthConfigError("oidc 모드에는 HWPCOWORK_OIDC_ISSUER / CLIENT_ID / RESOURCE_URL 이 필요하다.")
    principals = {}
    for e in load_json_list(env.get("HWPCOWORK_PRINCIPALS_FILE") or None):
        if "username" not in e or "label" not in e:
            raise AuthConfigError(f"principals 항목에 username·label 이 필요하다: {e!r}")
        roots = e.get("roots", [])
        if isinstance(roots, list):
            roots = frozenset(str(r).strip() for r in roots if str(r).strip()) & frozenset(known_roots)
        elif roots != "all":
            raise AuthConfigError(f"roots 는 all | [루트명...]: {roots!r}")
        u = str(e["username"]).strip()
        principals[u] = Principal(label=str(e["label"]), email=str(e.get("email") or f"{u}@hwp_cowork.local"),
                                  roots=roots, write=bool(e.get("write", False)), username=u)
    if principals and oidc is None:
        raise AuthConfigError("principals 가 있으면 HWPCOWORK_OIDC_ISSUER / CLIENT_ID / RESOURCE_URL 이 필요하다.")
    return Registry(principals, OIDCVerifier(oidc) if oidc else None), oidc
