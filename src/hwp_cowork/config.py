"""환경변수 → 설정. 접두사 HWPCOWORK_."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


def _roots(spec: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for part in filter(None, (x.strip() for x in spec.split(","))):
        name, _, path = part.partition("=")
        if not name or not path:
            raise ConfigError(f"HWPCOWORK_ROOTS 항목은 이름=경로: {part!r}")
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise ConfigError(f"루트 폴더가 없다: {name}={p}")
        out[name.strip()] = p
    if not out:
        raise ConfigError("HWPCOWORK_ROOTS 가 비어 있다 (예: docs=/srv/docs,shared=/srv/shared)")
    return out


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


@dataclass
class Config:
    roots: dict[str, Path]      # 문서 폴더 — 원본은 여기서 읽기만 한다 (사본 저장은 writable_roots 만)
    work_dir: Path              # 샌드박스. 사람별 폴더 아래 작업(job)마다 v0, v1 … 판
    data_dir: Path              # 링크 DB 등
    transport: str
    auth: str                   # none | token | oidc
    host: str
    port: int
    rhwp_bin: str
    rhwp_version: str | None
    keep_days: int
    public_base: str
    upload_base: str            # 업로드 링크 주소 — claude.ai 코드 실행의 허용 도메인에 넣는다
    studio_url: str             # 실시간 화면이 띄우는 rhwp-studio 주소
    studio_upstream: str | None # 있으면 /studio/ 를 이 주소에서 중계한다 — 실시간 화면과 같은 도메인이어야 studio 설정을 넣을 수 있다
    link_ttl_sec: int
    max_upload_mb: int
    nas_save_url: str | None    # (선택) 외부 저장 엔드포인트 — 원본 폴더에 직접 쓸 수 없을 때
    nas_internal_key: str | None
    run_timeout_sec: int
    allowed_hosts: list[str]
    allowed_origins: list[str]
    writable_roots: frozenset[str] = frozenset()  # 사본을 직접 써도 되는 루트 (나머지는 읽기 전용)
    studio_dir: Path | None = None  # rhwp-studio 빌드 산출물 (있으면 /studio/ 로 직접 낸다)

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        transport = e("HWPCOWORK_TRANSPORT", "stdio").strip().lower()
        if transport not in {"stdio", "http"}:
            raise ConfigError(f"HWPCOWORK_TRANSPORT 는 stdio 또는 http: {transport!r}")
        auth = (e("HWPCOWORK_AUTH") or ("oidc" if e("HWPCOWORK_OIDC_ISSUER") else
                                         "token" if (e("HWPCOWORK_TOKEN") or e("HWPCOWORK_TOKENS_FILE")) else "none")).strip().lower()
        if auth not in {"none", "token", "oidc"}:
            raise ConfigError(f"HWPCOWORK_AUTH 는 none | token | oidc: {auth!r}")
        work = Path(e("HWPCOWORK_WORK_DIR", "./work")).expanduser().resolve()
        data = Path(e("HWPCOWORK_DATA_DIR", "./data")).expanduser().resolve()
        work.mkdir(parents=True, exist_ok=True)
        data.mkdir(parents=True, exist_ok=True)
        rhwp = e("HWPCOWORK_RHWP_BIN") or "rhwp"
        public = e("HWPCOWORK_PUBLIC_BASE", "http://127.0.0.1:8119").rstrip("/")
        return cls(
            roots=_roots(e("HWPCOWORK_ROOTS", "")),
            work_dir=work, data_dir=data, transport=transport, auth=auth,
            host=e("HWPCOWORK_HOST", "127.0.0.1"), port=int(e("HWPCOWORK_PORT", "8119")),
            rhwp_bin=rhwp, rhwp_version=e("HWPCOWORK_RHWP_VERSION") or None,
            keep_days=int(e("HWPCOWORK_KEEP_DAYS", "30")),
            public_base=public,
            upload_base=(e("HWPCOWORK_UPLOAD_BASE") or public).rstrip("/"),
            studio_upstream=(e("HWPCOWORK_STUDIO_UPSTREAM") or "").rstrip("/") + "/" if e("HWPCOWORK_STUDIO_UPSTREAM") else None,
            studio_url=e("HWPCOWORK_STUDIO_URL") or f"{public}/studio/",
            studio_dir=Path(e("HWPCOWORK_STUDIO_DIR")).resolve() if e("HWPCOWORK_STUDIO_DIR") else None,
            link_ttl_sec=int(e("HWPCOWORK_LINK_TTL_MIN", "30")) * 60,
            max_upload_mb=int(e("HWPCOWORK_MAX_UPLOAD_MB", "100")),
            nas_save_url=e("HWPCOWORK_NAS_SAVE_URL") or None,
            nas_internal_key=e("HWPCOWORK_NAS_INTERNAL_KEY") or None,
            run_timeout_sec=int(e("HWPCOWORK_RUN_TIMEOUT_SEC", "300")),
            allowed_hosts=_list("HWPCOWORK_ALLOWED_HOSTS"),
            allowed_origins=_list("HWPCOWORK_ALLOWED_ORIGINS"),
            writable_roots=frozenset(_list("HWPCOWORK_WRITABLE_ROOTS")),
        )
