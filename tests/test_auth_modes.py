"""인증 모드 — none · token · oidc."""

import json

import pytest

from hwp_cowork.auth import LOCAL, build_registry
from hwp_cowork.oidc import AuthConfigError

TOK = "t" * 40


def test_none_mode_has_no_registry():
    assert build_registry(["docs"], {}, mode="none") == (None, None)


def test_token_mode_single_token():
    reg, oidc = build_registry(["docs"], {"HWPCOWORK_TOKEN": TOK}, mode="token")
    assert oidc is None
    assert reg.resolve({"Authorization": f"Bearer {TOK}"}) is LOCAL
    assert reg.resolve({"Authorization": "Bearer " + "x" * 40}) is None
    assert reg.resolve({}) is None


def test_token_mode_file_scopes_roots(tmp_path):
    f = tmp_path / "tokens.json"
    f.write_text(json.dumps([{"token": TOK, "label": "Bob", "roots": ["docs", "nope"], "write": False}]))
    reg, _ = build_registry(["docs", "other"], {"HWPCOWORK_TOKENS_FILE": str(f)}, mode="token")
    p = reg.resolve({"authorization": f"Bearer {TOK}"})
    assert p.label == "Bob" and p.can_see("docs") and not p.can_see("other") and not p.write


def test_token_mode_rejects_short_or_missing():
    with pytest.raises(AuthConfigError):
        build_registry(["docs"], {}, mode="token")
    with pytest.raises(AuthConfigError):
        build_registry(["docs"], {"HWPCOWORK_TOKEN": "short"}, mode="token")


def test_oidc_mode_requires_issuer():
    with pytest.raises(AuthConfigError):
        build_registry(["docs"], {}, mode="oidc")
