"""브라우저 링크 로그인(websso) — 쿠키 서명·주인 확인·대입 차단."""

import time

from starlette.requests import Request

from hwp_cowork.oidc import OIDCConfig
from hwp_cowork.websso import Throttle, WebSSO


class P:
    def __init__(self, label):
        self.label = label


def _req(cookies: dict | None = None, ip="1.2.3.4", path="/live/abcdEFGH"):
    hdr = [(b"cf-connecting-ip", ip.encode())]
    if cookies:
        hdr.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({"type": "http", "method": "GET", "path": path, "headers": hdr, "query_string": b"",
                    "client": ("127.0.0.1", 1), "server": ("x", 443), "scheme": "https"})


def _sso(tmp_path):
    oidc = OIDCConfig(issuer="https://auth.example/application/o/hwp/", client_id="cid",
                      resource_url="https://hwp.example/mcp")
    return WebSSO(oidc, "https://hwp.example", {"alice": P("Alice"), "bob": P("Bob")}, tmp_path, "hwp_sso")


def test_gate(tmp_path):
    sso = _sso(tmp_path)
    # 로그인 안 함 → 페이지는 로그인으로, API 는 401
    r = sso.gate(_req(), "alice", html_page=True)
    assert r.status_code == 302 and r.headers["location"].startswith("/sso/login?next=/live/")
    assert sso.gate(_req(), "alice").status_code == 401
    ok = sso.signer.dumps({"u": "alice", "exp": time.time() + 60})
    assert sso.gate(_req({"hwp_sso": ok}), "alice") is None
    assert sso.gate(_req({"hwp_sso": ok}), "bob").status_code == 403           # 다른 사람 링크
    assert sso.gate(_req({"hwp_sso": ok[:-2] + "xx"}), "alice").status_code == 401   # 위조
    old = sso.signer.dumps({"u": "alice", "exp": time.time() - 1})
    assert sso.gate(_req({"hwp_sso": old}), "alice").status_code == 401           # 만료
    assert sso.owner_of_label("Bob") == "bob"
    # 키는 재시작해도 같다
    assert _sso(tmp_path).signer.loads(ok)["u"] == "alice"
    assert WebSSO._next("//evil.com") == "/" and WebSSO._next("/live/x") == "/live/x"


def test_throttle():
    t = Throttle(limit=3, window=60)
    for _ in range(3):
        assert not t.blocked(_req())
        t.fail(_req())
    assert t.blocked(_req()) and not t.blocked(_req(ip="5.6.7.8"))
