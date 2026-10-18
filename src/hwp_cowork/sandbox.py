"""샌드박스 — 사람별 작업(job) 폴더와 판(v0, v1, …).

  <work>/<username>/<job>/
      job.json           출처(문서 경로)·이름·판 목록
      v0.hwp             폴더에서 가져온 원본 사본 (고치지 않는다)
      v1.hwp, v2.hwp …   편집할 때마다 새 판. 앞 판은 그대로 남는다 → undo·diff 가 된다
      out/               PDF·CSV·SVG 같은 부산물, 도구가 만든 기타 파일

원본은 이 서버에서 읽기 전용으로만 보인다. 폴더에 되돌려 넣는 일은 저장 기능이 한다 (사본 _vN).
작업은 마지막으로 손댄 뒤 keep_days 가 지나면 지운다.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DOC_EXT = {".hwp", ".hwpx", ".hml"}
_JOB_RE = re.compile(r"^[a-z0-9]{6}$")
_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


class SandboxError(ValueError):
    pass


@dataclass
class Version:
    v: int
    file: str
    at: str
    by: str              # 도구 이름 (open / hwp_set_cell / editor …)
    note: str = ""
    pages: list = field(default_factory=list)   # 이 판에서 바뀐 쪽 (1부터, rhwp changedPages) — 화면이 그 쪽으로 안내


@dataclass
class Job:
    id: str
    owner: str
    source: str          # 문서 가상 경로 (docs/…/예산안.hwp) 또는 "" (샌드박스에서 만든 것)
    name: str            # 원래 파일 이름
    ext: str
    created: str
    versions: list[Version] = field(default_factory=list)
    src_mtime: float = 0.0   # 가져올 때 원본의 수정시각 — 원본이 바뀌면 새 작업을 만든다
    live: dict = field(default_factory=dict)   # 실시간 화면 링크 {"code","url","exp"} — 작업마다 하나를 재사용
    dir: Path = field(default=Path("."), repr=False)

    @property
    def current(self) -> Version:
        return self.versions[-1]

    def path(self, v: int | None = None) -> Path:
        if v is None:
            return self.dir / self.current.file
        for x in self.versions:
            if x.v == v:
                return self.dir / x.file
        raise SandboxError(f"판 v{v} 이 없다. 있는 판: {[f'v{x.v}' for x in self.versions]}")

    @property
    def out(self) -> Path:
        p = self.dir / "out"
        p.mkdir(exist_ok=True)
        return p

    def summary(self) -> dict:
        return {"job": self.id, "name": self.name, "source": self.source or None,
                "current": f"v{self.current.v}", "versions": [
                    {"v": f"v{x.v}", "at": x.at, "by": x.by, **({"note": x.note} if x.note else {}),
                     **({"pages": x.pages} if x.pages else {})}
                    for x in self.versions],
                "files": sorted(p.name for p in self.out.iterdir() if p.is_file())}


class Sandbox:
    def __init__(self, work_dir: Path, keep_days: int = 7) -> None:
        self.base = work_dir
        self.keep_sec = keep_days * 86400
        self._lock = threading.Lock()

    # ---- 작업 --------------------------------------------------------------

    def user_dir(self, owner: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", owner):
            raise SandboxError(f"사용자 이름이 이상하다: {owner!r}")
        d = self.base / owner
        d.mkdir(parents=True, exist_ok=True)
        return d

    def for_source(self, owner: str, src: Path, source_vpath: str, create: bool = True) -> Job | None:
        """문서 폴더의 문서에 붙은 작업. 같은 원본(수정시각까지 같음)의 작업이 있으면 그것을 이어 쓰고,
        없으면 create=True 일 때 조용히 만든다 (사용자는 작업을 의식하지 않는다)."""
        mtime = src.stat().st_mtime
        for j in self.list(owner):
            if j.source == source_vpath and abs(j.src_mtime - mtime) < 1:
                return j
        return self.create(owner, src, source_vpath, src.name) if create else None

    def create(self, owner: str, src: Path, source_vpath: str, name: str) -> Job:
        ext = Path(name).suffix.lower()
        if ext not in DOC_EXT:
            raise SandboxError(f"한글 문서(.hwp .hwpx .hml)가 아니다: {name}")
        ud = self.user_dir(owner)
        with self._lock:
            while True:
                jid = "".join(secrets.choice(_ALPHABET) for _ in range(6))
                if not (ud / jid).exists():
                    break
            d = ud / jid
            d.mkdir()
        shutil.copy2(src, d / f"v0{ext}")
        job = Job(id=jid, owner=owner, source=source_vpath, name=name, ext=ext, created=_now(),
                  versions=[Version(0, f"v0{ext}", _now(), "open", "원본 사본")], dir=d,
                  src_mtime=src.stat().st_mtime)
        self._save(job)
        return job

    def get(self, owner: str, jid: str) -> Job:
        if not _JOB_RE.fullmatch(jid or ""):
            raise SandboxError(f"작업 ID 형식이 아니다: {jid!r} (hwp_jobs 로 확인)")
        d = self.user_dir(owner) / jid
        f = d / "job.json"
        if not f.is_file():
            raise SandboxError(f"작업 {jid} 이 없다 — 만료됐거나({self.keep_sec // 86400}일) 다른 사람 것. 같은 문서 경로로 다시 부르면 새 사본이 생긴다.")
        raw = json.loads(f.read_text(encoding="utf-8"))
        vs = [Version(**x) for x in raw.pop("versions")]
        return Job(**raw, versions=vs, dir=d)

    def list(self, owner: str) -> list[Job]:
        out = []
        for d in sorted(self.user_dir(owner).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if (d / "job.json").is_file():
                try:
                    out.append(self.get(owner, d.name))
                except (SandboxError, ValueError, TypeError, KeyError):
                    continue
        return out

    def _save(self, job: Job) -> None:
        data = {"id": job.id, "owner": job.owner, "source": job.source, "name": job.name, "ext": job.ext,
                "created": job.created, "versions": [x.__dict__ for x in job.versions], "src_mtime": job.src_mtime,
                "live": job.live}
        tmp = job.dir / ".job.json.part"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(job.dir / "job.json")

    # ---- 판 ----------------------------------------------------------------

    def next_path(self, job: Job, ext: str | None = None) -> Path:
        """다음 판이 놓일 자리 (아직 등록하지 않는다)."""
        return job.dir / f"v{job.current.v + 1}{ext or job.ext}"

    def commit(self, job: Job, produced: Path, by: str, note: str = "") -> Version:
        """produced(샌드박스 안 파일)를 다음 판으로 등록한다."""
        target = self.next_path(job, produced.suffix.lower() if produced.suffix.lower() in DOC_EXT else None)
        if produced.resolve() != target.resolve():
            produced.replace(target)
        v = Version(job.current.v + 1, target.name, _now(), by, note[:200])
        job.versions.append(v)
        self._save(job)
        return v

    def amend(self, job: Job, produced: Path, by: str, note: str = "") -> Version:
        """마지막 판을 이 파일로 바꾼다 — 에디터 자동 저장이 연속 편집을 한 판으로 묶을 때."""
        last = job.current
        produced.replace(job.dir / last.file)
        last.at, last.by, last.note = _now(), by, note[:200]
        self._save(job)
        return last

    def save_meta(self, job: Job) -> None:
        self._save(job)

    # ---- 편집 중 표시 (실시간 화면이 "Claude 편집 중" 을 띄운다) ------------------------

    @staticmethod
    def busy(job: Job, on: bool, what: str = "") -> None:
        f = job.dir / ".busy"
        if on:
            f.write_text(what, encoding="utf-8")
        else:
            f.unlink(missing_ok=True)

    @staticmethod
    def busy_state(job: Job) -> str | None:
        f = job.dir / ".busy"
        try:
            if time.time() - f.stat().st_mtime > 600:     # 10분 넘게 남은 표시는 죽은 것
                return None
            return f.read_text(encoding="utf-8") or "편집"
        except OSError:
            return None

    def undo(self, job: Job) -> Version:
        if len(job.versions) <= 1:
            raise SandboxError("되돌릴 편집이 없다 (v0 은 원본 사본).")
        last = job.versions.pop()
        (job.dir / last.file).unlink(missing_ok=True)
        self._save(job)
        return last

    # ---- 정리 --------------------------------------------------------------

    def sweep(self) -> int:
        """keep_days 동안 손대지 않은 작업을 지운다."""
        cutoff = time.time() - self.keep_sec
        n = 0
        for ud in self.base.iterdir() if self.base.is_dir() else []:
            if not ud.is_dir():
                continue
            for d in ud.iterdir():
                if not (d / "job.json").is_file():
                    continue
                newest = max((p.stat().st_mtime for p in d.rglob("*")), default=d.stat().st_mtime)
                if newest < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
        return n

    def start_sweeper(self, interval_sec: int = 3600) -> None:
        def loop() -> None:
            while True:
                try:
                    self.sweep()
                except OSError:
                    pass
                time.sleep(interval_sec)
        threading.Thread(target=loop, name="sandbox-sweep", daemon=True).start()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
