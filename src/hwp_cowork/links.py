"""짧은 링크 — 채팅창에서 클릭해 샌드박스 문서를 보거나(view) 내려받는(dl) 일회성 URL.

커넥터(claude.ai)는 파일 바이트를 도구로 주고받지 못한다. 그래서 서버가 짧게 사는 URL 을 만들고
사람이 브라우저로 그 URL 을 연다.

발급은 **8자 코드** (`/view/Ab3kQ9xZ`) — 코드↔payload(경로·발급자·만료·쓰기권한) 를 SQLite 에 TTL 로 보관한다.
payload 전체를 URL 에 싣는 서명 방식은 한글 경로면 430자가 넘어 채팅에 붙이기 불편해서 짧은 코드로 한다.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"   # 0/O/1/l/I 제외
_CODE_LEN = 8


class LinkError(ValueError):
    pass


@dataclass(frozen=True)
class Link:
    kind: str        # dl | up
    path: str        # 가상 경로 (dl: 파일, up: 폴더)
    who: str
    exp: int
    rw: bool = False   # 이 링크로 저장까지 되는가 (발급자의 write 권한)


class LinkSigner:
    def __init__(self, public_base: str, ttl_sec: int = 1800, db_path: str | Path | None = None) -> None:
        self.base = public_base.rstrip("/")
        self.ttl = ttl_sec
        # 짧은 코드 저장소. db_path 가 없으면 메모리 (테스트용 — 재시작하면 링크가 죽는다)
        self._db = sqlite3.connect(str(db_path) if db_path else ":memory:", check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS links (code TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                         "payload TEXT NOT NULL, exp INTEGER NOT NULL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS links_exp ON links(exp)")
        self._db.commit()
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def make(self, kind: str, path: str, who: str, rw: bool = False, ttl: int | None = None,
             base: str | None = None) -> tuple[str, int]:
        exp = int(time.time()) + (ttl or self.ttl)
        payload = json.dumps({"k": kind, "p": path, "w": who, "e": exp, "rw": rw}, ensure_ascii=False)
        with self._lock:
            self._sweep()
            for _ in range(5):   # 충돌은 사실상 없지만(57^8) 있어도 다시 뽑는다
                code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
                try:
                    self._db.execute("INSERT INTO links VALUES (?,?,?,?)", (code, kind, payload, exp))
                    self._db.commit()
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                raise LinkError("링크 코드를 만들지 못했다")
        return f"{(base or self.base).rstrip('/')}/{kind}/{code}", exp

    def _sweep(self) -> None:
        now = time.time()
        if now - self._last_sweep < 300:
            return
        self._db.execute("DELETE FROM links WHERE exp < ?", (int(now),))
        self._db.commit()
        self._last_sweep = now

    def verify(self, kind: str, code: str) -> Link:
        if len(code) != _CODE_LEN or any(c not in _CODE_ALPHABET for c in code):
            raise LinkError("링크 형식이 아니다")
        with self._lock:
            row = self._db.execute("SELECT kind, payload, exp FROM links WHERE code = ?", (code,)).fetchone()
        if row is None:
            raise LinkError("없는 링크다 — 만료됐거나 잘못된 주소. 다시 만들어 달라고 할 것")
        if row[0] != kind:
            raise LinkError("링크 종류가 다르다")
        if int(row[2]) < time.time():
            raise LinkError("만료된 링크다 — 다시 만들어 달라고 할 것")
        d = json.loads(row[1])
        return Link(kind=d["k"], path=d["p"], who=d["w"], exp=int(d["e"]), rw=bool(d.get("rw")))
