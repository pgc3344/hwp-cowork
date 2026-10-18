"""고정한 rhwp 버전과 상류 릴리스의 차이를 본다 — 자동으로 올리지 않되, 방치도 하지 않는다.

하루 한 번 GitHub 릴리스 목록을 읽어 고정 태그보다 새 정식 릴리스가 몇 개인지 센다. 이만큼 벌어지면 경고한다:
  * 새 릴리스가 WARN_BEHIND 개 이상, 또는
  * 부 버전(0.8 → 0.9)·주 버전이 바뀌었을 때
경고는 roots 도구 응답(rhwp.warning)과 서버 로그에 나온다. 올리는 일은 Dockerfile 의 RHWP_VERSION 을 고쳐 푸시하는 것.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import asdict, dataclass

import httpx

log = logging.getLogger("hwp_cowork.rhwp")

RELEASES = "https://api.github.com/repos/edwardkim/rhwp/releases?per_page=50"
WARN_BEHIND = 3
INTERVAL_SEC = 24 * 3600


@dataclass
class UpdateState:
    pinned: str | None
    latest: str | None = None
    behind: int | None = None
    checked_at: str | None = None
    warning: str | None = None
    error: str | None = None


def _ver(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", tag)[:3])


def assess(pinned: str, tags: list[str]) -> UpdateState:
    """tags: 정식 릴리스 태그, 최신이 앞. 순수 함수 — 테스트한다."""
    st = UpdateState(pinned=pinned, latest=tags[0] if tags else None)
    newer = [t for t in tags if _ver(t) > _ver(pinned)]
    st.behind = len(newer)
    if not newer:
        return st
    lv, pv = _ver(newer[0]), _ver(pinned)
    reasons = []
    if st.behind >= WARN_BEHIND:
        reasons.append(f"새 릴리스 {st.behind}개")
    if lv[:2] != pv[:2]:
        reasons.append(f"부 버전 변경 {pinned} → {newer[0]}")
    if reasons:
        st.warning = (f"rhwp 가 상류보다 많이 뒤처졌다 ({', '.join(reasons)}). "
                      f"Dockerfile 의 RHWP_VERSION 을 올리고 한글 문서를 열어·고쳐·저장까지 확인할 것. "
                      f"https://github.com/edwardkim/rhwp/releases/tag/{newer[0]}")
    return st


class UpdateChecker:
    def __init__(self, pinned: str | None) -> None:
        self.state = UpdateState(pinned=pinned)

    def check(self) -> UpdateState:
        if not self.state.pinned:
            return self.state
        try:
            r = httpx.get(RELEASES, timeout=15, headers={"User-Agent": "hwp-cowork", "Accept": "application/vnd.github+json"})
            r.raise_for_status()
            tags = [x["tag_name"] for x in r.json() if not x.get("prerelease") and not x.get("draft")]
            tags.sort(key=_ver, reverse=True)
            st = assess(self.state.pinned, tags)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            st = UpdateState(pinned=self.state.pinned, latest=self.state.latest, behind=self.state.behind,
                             warning=self.state.warning, error=f"상류 확인 실패: {e}")
        st.checked_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.state = st
        if st.warning:
            log.warning(st.warning)
        return st

    def start_background(self) -> None:
        if not self.state.pinned:
            return

        def loop() -> None:
            while True:
                self.check()
                time.sleep(INTERVAL_SEC)
        threading.Thread(target=loop, name="rhwp-update", daemon=True).start()

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self.state).items() if v is not None}
