"""경로 안전장치. 커넥터가 주는 경로는 항상 `<루트이름>/상대/경로` 꼴이고, 실제 파일시스템 경로는
루트 아래를 벗어날 수 없다. 심볼릭 링크로 밖으로 나가는 것도 막는다."""

from __future__ import annotations

import unicodedata
from pathlib import Path, PurePosixPath

# NAS(Synology 등)가 만드는 잡동사니 + 숨김 파일은 보이지 않는다
HIDDEN_DIRS = {"@eaDir", "#recycle", "#snapshot", "@tmp", ".git", "node_modules", "__pycache__"}
HIDDEN_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


class PathError(ValueError):
    pass


def nfc(s: str) -> str:
    """Mac 이 SMB 로 쓴 파일명은 NFD 로 남는 일이 있다 — 비교·색인은 항상 NFC 로."""
    return unicodedata.normalize("NFC", s)


def is_hidden(name: str) -> bool:
    return name in HIDDEN_DIRS or name in HIDDEN_FILES or name.startswith(".") or name.startswith("~$")


def split(vpath: str) -> tuple[str, str]:
    """`docs/2027/a.hwp` → ("docs", "2027/a.hwp"). 루트만 주면 rel="" ."""
    v = nfc((vpath or "").strip()).replace("\\", "/").strip("/")
    if not v:
        raise PathError("경로가 비어 있다. `<루트>/경로` 꼴로 줄 것 (루트 목록은 roots 도구).")
    parts = [p for p in PurePosixPath(v).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathError("`..` 는 쓸 수 없다.")
    return parts[0], "/".join(parts[1:])


def resolve(roots: dict[str, Path], vpath: str) -> tuple[str, str, Path]:
    """가상 경로 → (루트이름, 상대경로, 실제 Path). 루트 밖·숨김·링크 탈출은 거부."""
    root, rel = split(vpath)
    if root not in roots:
        raise PathError(f"루트 {root!r} 가 없다. 있는 루트: {sorted(roots)}")
    base = roots[root]
    real = (base / rel).resolve() if rel else base
    try:
        real.relative_to(base)
    except ValueError:
        raise PathError(f"루트 밖으로 나가는 경로다: {vpath}") from None
    if rel and any(is_hidden(p) for p in PurePosixPath(rel).parts):
        raise PathError(f"숨김·시스템 경로다: {vpath}")
    return root, rel, real


def vpath(root: str, rel: str) -> str:
    return f"{root}/{rel}" if rel else root
