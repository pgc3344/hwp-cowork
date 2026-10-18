"""브라우저 링크 로그인 — 짧은 링크(/live, /view …)를 연 사람이 링크 주인인지 OIDC 로그인으로 확인한다.

링크 코드만으로는 열리지 않는다: 코드 + 링크 주인 계정의 로그인 쿠키가 둘 다 있어야 한다. 링크가 새도
(채팅 기록·캡처·전달) 로그인하지 않은 사람은 아무것도 못 한다.

로그인: OIDC Authorization Code + PKCE (MCP 커넥터와 같은 public provider, secret 없음).
  콜백 `<public>/sso/callback` 이 OIDC 클라이언트의 redirect URI 에 있어야 한다.
세션: 서명한 쿠키 (HttpOnly·Secure·SameSite=Lax, 90일). 서명 키는 data 폴더에 한 번 만들어 둔다.
Throttle: 없는 링크 코드를 계속 두드리는 IP 를 잠시 막는다 (다운로드 링크처럼 로그인 없이 열리는 것 보호).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .oidc import OIDCConfig, OIDCVerifier

log = logging.getLogger(__name__)

SESSION_TTL = 90 * 86400   # 한 번 로그인한 기기는 석 달 동안 다시 묻지 않는다
OAUTH_TTL = 600


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class _Signer:
    def __init__(self, key: bytes) -> None:
        self.key = key

    def _mac(self, body: str) -> str:
        return _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())

    def dumps(self, data: dict[str, Any]) -> str:
        body = _b64(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode())
        return f"{body}.{self._mac(body)}"

    def loads(self, raw: str | None) -> dict[str, Any] | None:
        if not raw or raw.count(".") != 1:
            return None
        body, mac = raw.split(".")
        if not hmac.compare_digest(mac, self._mac(body)):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < time.time():
            return None
        return data


def _load_key(path: Path) -> bytes:
    try:
        k = path.read_bytes()
        if len(k) >= 32:
            return k
    except OSError:
        pass
    k = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(k)
    tmp.chmod(0o600)
    tmp.replace(path)
    return k


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><body style='font-family:system-ui,-apple-system,sans-serif;"
        f"padding:2rem 1rem;max-width:32rem;margin:auto;line-height:1.6'>{body}</body>",
        status, headers={"Cache-Control": "no-store"})


class WebSSO:
    def __init__(self, oidc: OIDCConfig, public_url: str, principals: dict[str, Any], data_dir: Path,
                 cookie: str) -> None:
        self.oidc, self.principals, self.cookie = oidc, principals, cookie
        self.public = public_url.rstrip("/")
        self.redirect_uri = self.public + "/sso/callback"
        self.signer = _Signer(_load_key(Path(data_dir) / "websso.key"))
        self.verifier = OIDCVerifier(oidc)
        self._discovery: tuple[float, dict[str, Any]] | None = None
        self._secure = self.public.startswith("https://")

    # ---- 확인 -----------------------------------------------------------------

    def user(self, request: Request) -> str | None:
        data = self.signer.loads(request.cookies.get(self.cookie))
        u = data.get("u") if data else None
        return u if u in self.principals else None

    def gate(self, request: Request, owner: str, *, html_page: bool = False) -> Response | None:
        """owner(username) 로 로그인한 브라우저가 아니면 막는 응답, 맞으면 None.
        html_page=True 면 로그인 화면으로 보낸다 (링크를 처음 여는 GET). API 호출은 401/403 JSON."""
        from starlette.responses import JSONResponse
        u = self.user(request)
        if u == owner:
            return None
        if u is None:
            if html_page:
                return RedirectResponse(f"/sso/login?next={quote(request.url.path)}", status_code=302)
            return JSONResponse({"error": "로그인이 필요하다 — 페이지를 새로 고칠 것"}, status_code=401)
        me = getattr(self.principals.get(u), "label", u)
        them = getattr(self.principals.get(owner), "label", owner)
        if html_page:
            nxt = quote(request.url.path)
            return page("다른 계정의 링크", f"<h3>다른 사람의 링크예요</h3><p>이 링크는 <b>{html.escape(them)}</b> 님 것이고, "
                        f"지금 브라우저는 <b>{html.escape(me)}</b> 님으로 로그인돼 있어요.</p>"
                        f"<p><a href='/sso/logout?next={nxt}'>로그아웃하고 다른 계정으로 로그인</a></p>", 403)
        return JSONResponse({"error": "다른 계정의 링크다"}, status_code=403)

    def owner_of_label(self, label: str) -> str | None:
        for u, p in self.principals.items():
            if getattr(p, "label", None) == label:
                return u
        return None

    # ---- 로그인 흐름 ------------------------------------------------------------

    async def _endpoints(self) -> dict[str, Any]:
        now = time.time()
        if self._discovery and self._discovery[0] > now:
            return self._discovery[1]
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(self.oidc.issuer.rstrip("/") + "/.well-known/openid-configuration")
            r.raise_for_status()
            doc = r.json()
        self._discovery = (now + 3600, doc)
        return doc

    def _set(self, resp: Response, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(name, value, max_age=max_age, path="/", httponly=True, secure=self._secure, samesite="lax")

    @staticmethod
    def _next(raw: str | None) -> str:
        # 같은 사이트 안 경로만 (열린 리디렉트 방지)
        return raw if raw and raw.startswith("/") and not raw.startswith("//") and "\\" not in raw else "/"

    async def login(self, request: Request) -> Response:
        try:
            ep = await self._endpoints()
        except (httpx.HTTPError, ValueError) as e:
            log.error("OIDC discovery 실패: %s", e)
            return page("오류", "<h3>로그인 서버에 닿지 못했어요</h3><p>잠시 뒤 다시 열어 주세요.</p>", 502)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        q = urlencode({"response_type": "code", "client_id": self.oidc.client_id, "redirect_uri": self.redirect_uri,
                       "scope": "openid profile", "state": state, "code_challenge": challenge,
                       "code_challenge_method": "S256"})
        resp = RedirectResponse(f"{ep['authorization_endpoint']}?{q}", status_code=302)
        pending = {"s": state, "v": verifier, "n": self._next(request.query_params.get("next")),
                   "exp": time.time() + OAUTH_TTL}
        self._set(resp, self.cookie + "_oauth", self.signer.dumps(pending), OAUTH_TTL)
        return resp

    async def callback(self, request: Request) -> Response:
        pending = self.signer.loads(request.cookies.get(self.cookie + "_oauth"))
        code, state = request.query_params.get("code"), request.query_params.get("state")
        retry = "<p><a href='/sso/login'>다시 시도</a></p>"
        if not pending or not code or not state or not hmac.compare_digest(state, pending.get("s", "")):
            return page("로그인 실패", "<h3>로그인에 실패했어요</h3><p>링크를 다시 열어 주세요.</p>", 400)
        try:
            ep = await self._endpoints()
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(ep["token_endpoint"], data={
                    "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                    "client_id": self.oidc.client_id, "code_verifier": pending["v"]})
            tok = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as e:
            log.error("토큰 교환 실패: %s", e)
            tok = {}
        got = await asyncio.to_thread(self.verifier.username, tok["access_token"]) if tok.get("access_token") else None
        if got is None:
            return page("로그인 실패", "<h3>로그인에 실패했어요</h3>" + retry, 400)
        name = got[0]
        if name not in self.principals:
            log.warning("웹 로그인: principals 에 없는 사용자 %s", name)
            return page("권한 없음", "<h3>이 서비스를 쓸 수 있는 계정이 아니에요</h3>", 403)
        log.info("웹 로그인: %s", name)
        resp = RedirectResponse(pending.get("n") or "/", status_code=303)
        self._set(resp, self.cookie, self.signer.dumps({"u": name, "exp": time.time() + SESSION_TTL}), SESSION_TTL)
        resp.delete_cookie(self.cookie + "_oauth", path="/")
        return resp

    async def logout(self, request: Request) -> Response:
        nxt = self._next(request.query_params.get("next"))
        resp = RedirectResponse(f"/sso/login?next={quote(nxt)}", status_code=303)
        resp.delete_cookie(self.cookie, path="/")
        return resp

    def register(self, mcp) -> None:
        mcp.custom_route("/sso/login", methods=["GET"])(self.login)
        mcp.custom_route("/sso/callback", methods=["GET"])(self.callback)
        mcp.custom_route("/sso/logout", methods=["GET"])(self.logout)


class Throttle:
    """IP 마다 틀린 링크 코드 횟수를 센다. window 안에 limit 번 넘게 틀리면 그 IP 를 window 동안 막는다."""

    def __init__(self, limit: int = 20, window: float = 600) -> None:
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def ip(request: Request) -> str:
        return (request.headers.get("cf-connecting-ip")
                or (request.client.host if request.client else "") or "?")

    def blocked(self, request: Request) -> bool:
        ip, now = self.ip(request), time.time()
        with self._lock:
            xs = [t for t in self._fails.get(ip, []) if now - t < self.window]
            self._fails[ip] = xs
            return len(xs) >= self.limit

    def fail(self, request: Request) -> None:
        ip, now = self.ip(request), time.time()
        with self._lock:
            if len(self._fails) > 4096:
                self._fails.clear()
            self._fails.setdefault(ip, []).append(now)
            if len(self._fails[ip]) >= self.limit:
                log.warning("링크 코드 대입 의심 — %s 를 %d분 막는다", ip, int(self.window // 60))
