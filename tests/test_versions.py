"""rhwp 판이 한 곳에서만 어긋나지 않게 — Dockerfile RHWP_VERSION 과 레포에 커밋한 @rhwp/editor SDK 사본.
이미지는 빌드 때 npm 에서 받아 덮어쓰지만, 레포 사본(로컬 실행·공개 저장소 열람)도 같은 판이어야 한다."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _rhwp_version() -> str:
    m = re.search(r"^ARG RHWP_VERSION=v(\S+)$", (ROOT / "Dockerfile").read_text(), re.M)
    assert m, "Dockerfile 에 ARG RHWP_VERSION=vX.Y.Z 가 있어야 한다"
    return m.group(1)


def test_notice_links_same_rhwp_tag():
    notice = (ROOT / "NOTICE.md").read_text()
    assert f"rhwp/blob/v{_rhwp_version()}/THIRD_PARTY_LICENSES.md" in notice, "NOTICE.md 의 THIRD_PARTY_LICENSES 링크를 RHWP_VERSION 태그로"


def test_editor_sdk_matches_rhwp_version():
    sdk = (ROOT / "src/hwp_cowork/static/rhwp-editor/VERSION").read_text()
    assert sdk.startswith(f"@rhwp/editor {_rhwp_version()} "), \
        f"SDK 사본({sdk.split('(')[0].strip()})을 RHWP_VERSION({_rhwp_version()}) 의 npm 타르볼로 바꿀 것"
