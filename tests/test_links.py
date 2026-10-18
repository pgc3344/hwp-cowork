"""짧은 링크 — 코드 발급·조회·만료·종류 검사."""

import time

import pytest

from hwp_cowork.links import LinkError, LinkSigner


def test_short_code_roundtrip(tmp_path):
    s = LinkSigner("https://nas.example", ttl_sec=60, db_path=tmp_path / "links.sqlite3")
    url, exp = s.make("view", "DOCS/2027/아주 긴 한글 경로/2027년 예산요구 설명자료(여수신항).hwp", "Bob", rw=True)
    assert url.startswith("https://nas.example/view/") and len(url) < 50
    code = url.rsplit("/", 1)[1]
    link = s.verify("view", code)
    assert link.path.endswith(".hwp") and link.who == "Bob" and link.rw is True and link.exp == exp
    # 다른 인스턴스(재시작)도 같은 DB 면 읽는다
    s2 = LinkSigner("https://nas.example", ttl_sec=60, db_path=tmp_path / "links.sqlite3")
    assert s2.verify("view", code).path == link.path


def test_kind_mismatch_and_unknown(tmp_path):
    s = LinkSigner("http://x", db_path=tmp_path / "l.db")
    url, _ = s.make("dl", "DOCS/a.txt", "me")
    code = url.rsplit("/", 1)[1]
    with pytest.raises(LinkError, match="종류"):
        s.verify("view", code)
    with pytest.raises(LinkError, match="없는 링크"):
        s.verify("dl", "ZZZZZZZZ")
    with pytest.raises(LinkError, match="형식"):
        s.verify("dl", "short")


def test_expiry(tmp_path):
    s = LinkSigner("http://x", ttl_sec=1, db_path=tmp_path / "l.db")
    url, _ = s.make("up", "DOCS", "me")
    code = url.rsplit("/", 1)[1]
    time.sleep(1.2)
    with pytest.raises(LinkError, match="만료"):
        s.verify("up", code)

