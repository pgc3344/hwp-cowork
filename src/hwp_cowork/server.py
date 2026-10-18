"""hwp-cowork — 한글(.hwp/.hwpx) 문서를 각 잡고 처리하는 MCP 서버.

여기는 문서 폴더의 한글 문서를 (뒤에서 조용히 샌드박스 사본을 만들어) 조사·편집하고,
판(v1, v2 …)을 쌓아 검증한 뒤 폴더에 사본(_vN)으로 돌려놓는다. rhwp 의 모든 기능을 hwp_catalog → hwp_run 으로 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from importlib import resources
from typing import Any

import httpx
from mcp.server.apps import APP_MIME_TYPE, Apps
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .websso import Throttle, WebSSO
from .auth import LOCAL, OPEN_PATHS, AuthConfigError, Principal, Registry, build_registry
from .config import Config, ConfigError
from .fsafe import PathError, resolve
from .links import LinkError, LinkSigner
from .rhwp_update import UpdateChecker
from .runner import Manifest, RunError, Runner
from .sandbox import DOC_EXT, Job, Sandbox, SandboxError

log = logging.getLogger("hwp-cowork")

APP_URI = "ui://hwp/viewer.html"

INSTRUCTIONS = """\
한글(.hwp/.hwpx) 문서를 조사·편집하는 서버. 문서는 `<루트>/경로` 꼴의 문서 경로를 그대로 doc 에 넣으면 된다
(예: doc="docs/2027/예산안.hwp"). 따로 가져오거나 여는 단계는 없다 — 사용자에게 묻지 말고 바로 쓴다.

원본은 절대 고치지 않는다. 편집하면 서버가 뒤에서 사본을 만들어 판(v1, v2 …)을 쌓고, 같은 문서를 다시 고치면
그 판 위에 이어진다. 편집한 뒤 같은 doc 으로 읽으면 최신 판을 읽는다. 응답의 job 은 그 사본의 ID (doc 대신 써도 된다).

  조사: hwp_info(규모) · hwp_locate(어느 쪽·어느 표 칸) · hwp_tables(표 격자·좌표) · hwp_fields(누름틀) ·
        hwp_structure(개요) · hwp_extract(금액·날짜) · hwp_text(본문)
  편집: hwp_fill_fields · hwp_set_cell · hwp_replace_text  → 새 판
  새 문서: hwp_templates(견본·작성 규범) → hwp_new(name, spec) → 원본 없이 서식까지 갖춘 공문서·보고서 (표지·목차·□❍- 위계·음영 표·요약 상자·쪽번호). 이어서 그 job 으로 편집.
  확인: hwp_preview(쪽 이미지로 직접 보기) · hwp_diff(기본: 원본 대 지금) · hwp_undo · hwp_view_link(실시간 화면)
  반출: hwp_save_copy(원본 옆에 `이름_vN.hwp` 사본) · hwp_download_link
  첨부: 사용자가 채팅에 한글 파일(.hwp .hwpx)을 **첨부**하면 묻지 말고 곧바로 hwp_upload_link → 코드 실행으로 curl 업로드
        → 돌아온 job 으로 처리한다. 업로드가 일시 오류(5xx)면 같은 curl 을 한 번만 다시.
        **코드 실행으로 한글 파일의 압축을 풀거나 XML·PrvImage.png 를 직접 읽는 것은 금지** — hwp 도구(hwp_text·hwp_info·
        hwp_tables·hwp_preview)가 모두 실패했을 때만 최후의 수단으로 쓰고, 그때는 그렇게 했다고 한 줄 알린다.
        (직접 풀면 표·서식·쪽이 틀리고, 판 기록·편집·폴더 저장으로 이어지지 않는다)

일하는 방식: 알아서 끝까지 하고 결과만 짧게 말한다. 쓸 수 있는 방법을 늘어놓거나 조언하지 않는다.
사용자가 해 줘야만 진행되는 일(허용 도메인 추가·파일 놓기·로그인 등)이 있으면 그것 하나만 한 줄로 부탁하고 멈춘다.
  예) curl 업로드가 거부됨 → 다시 시도하지 말고 "위 칸에 파일을 끌어다 놓아 주세요." 한 줄.

그 밖의 모든 한글 기능(표 병합·행열·각주·머리말·쪽 설정·그림·도장·수식·차트·개인정보 가리기·PDF 등 150여 개):
  hwp_catalog("표 병합") 으로 이름·인자를 찾고 → hwp_run(tool, args, doc). 출력 인자는 파일 이름만 준다.
  여러 문서를 훑는 읽기 기능은 doc 없이 args 에 문서 폴더를 준다 (예: hwp_batch_search {paths:["docs/2027"], query:"위임전결"}).

채팅 안 화면(MCP Apps 를 지원하는 클라이언트 — claude.ai·Claude 데스크톱 등): 편집·미리보기 도구를 부르면 그 결과 아래에 쪽 미리보기가 뜨고
Claude 가 고치는 동안 저절로 바뀐다. 이때는 링크를 따로 주지 않아도 된다 — 사용자가 큰 화면을 원하면 그 화면의 "편집기" 버튼이나 아래 링크.

실시간 화면: 편집 결과에 live_view 링크가 온다. 사용자가 수정을 보고 싶어 하거나 결과를 보여줄 때는 **답변 맨 위에 한 줄로**
"실시간으로 보기: <링크>" 를 준다 (매 답변마다 반복하지 않는다). 그 화면에서 Claude 의 편집이 바로 보이고, 판 비교·되돌리기,
사람이 직접 고치기(자동으로 새 판), 폴더에 최종본 적용(사본 또는 원본 교체)이 된다. 모바일은 링크 없이 대화만으로도 된다.
표 칸을 고치기 전에 hwp_tables 로 좌표를 확인한다. 편집 뒤에는 hwp_diff 로 바뀐 곳을 사용자에게 보여주고,
폴더에 둘지는 사용자가 원할 때 hwp_save_copy 로. 손대지 않은 사본은 30일 뒤 지워진다.
"""


def build(cfg: Config, registry: Registry | None = None) -> tuple[MCPServer, Sandbox, UpdateChecker]:
    sandbox = Sandbox(cfg.work_dir, cfg.keep_days)
    manifest = Manifest(cfg.rhwp_bin)
    runner = Runner(cfg.rhwp_bin, manifest, sandbox, cfg.roots, cfg.run_timeout_sec)
    signer = LinkSigner(cfg.public_base, cfg.link_ttl_sec, db_path=cfg.data_dir / "links.sqlite3")
    updates = UpdateChecker(cfg.rhwp_version)
    # MCP Apps: 편집·미리보기 도구 결과가 채팅 안 화면(ui://hwp/viewer.html)으로도 뜬다. 지원 안 하는 클라이언트는 그냥 JSON.
    mcp = MCPServer("hwp-cowork", instructions=INSTRUCTIONS, version=__version__, extensions=[Apps()])
    UI = {"ui": {"resourceUri": APP_URI}}
    APP_ONLY = {"ui": {"resourceUri": APP_URI, "visibility": ["app"]}}     # 화면만 부른다 (Claude 목록엔 안 보인다)
    ro = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    wr = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
    all_roots = list(cfg.roots)

    def _who(ctx: Context) -> Principal:
        if registry is None:
            return LOCAL
        who = registry.resolve(ctx.headers)
        if who is None:
            raise ToolError("인증 주체를 확인할 수 없다.")
        return who

    def _job(who: Principal, job: str) -> Job:
        try:
            return sandbox.get(who.username, job)
        except SandboxError as e:
            raise ToolError(str(e)) from None

    HUMAN_WAIT = 12      # 사람이 실시간 화면에서 고치는 중이면 자동 저장(4초)을 이만큼 기다린다

    def _human(job: Job) -> dict | None:
        """실시간 화면이 2.5초마다 보내는 상태. 8초 안의 것만 산 것으로 본다."""
        try:
            h = json.loads((job.dir / ".human").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return h if time.time() - h.get("t", 0) < 8 else None

    def _human_news(who: Principal, job: Job) -> list[dict]:
        """Claude 가 이 문서를 마지막으로 본 뒤 사람이 만든 판 (직접 편집·되돌림) 과 바뀐 곳 몇 개."""
        seen = int(job.live.get("claude_seen", job.current.v))
        news = []
        for i, v in enumerate(job.versions):
            if v.v <= seen or not (v.by.startswith("editor") or v.by == "restore") or i == 0:
                continue
            item = {"version": f"v{v.v}", "by": "사용자(직접 편집)" if v.by.startswith("editor") else "사용자(되돌림)",
                    "at": v.at, "note": v.note}
            try:
                d = _text_diff(who.username, job, f"v{job.versions[i - 1].v}", f"v{v.v}", limit=5)
                item["changes"] = [{"page": c["page"], "old": c["old"], "new": c["new"]} for c in d["changes"]]
                item["change_count"] = d["count"]
            except Exception:  # noqa: BLE001 — 요약 실패는 편집을 막지 않는다
                pass
            news.append(item)
        return news

    def _run(who: Principal, tool: str, args: dict, job: Job | None, post=None) -> dict[str, Any]:
        t = manifest.tools.get(tool)
        news: list[dict] = []
        if job is not None:
            if t is not None and not t.read_only:
                # 사람이 화면에서 저장 안 된 수정을 하고 있으면 자동 저장을 기다린다 — 그 위에서 고치기 위해
                waited = 0
                while (h := _human(job)) and h.get("dirty") and waited < HUMAN_WAIT:
                    time.sleep(1); waited += 1
                if (h := _human(job)) and h.get("dirty"):
                    raise ToolError("사용자가 실시간 화면에서 이 문서를 직접 고치는 중이다 (저장 전). 덮어쓰지 않도록 편집을 멈췄다 — "
                                    "사용자에게 알리고, 손을 멈추면 몇 초 뒤 자동 저장되니 그다음 다시 할 것.")
                job = sandbox.get(job.owner, job.id)          # 기다리는 사이 생긴 판 위에서
            news = _human_news(who, job)
        try:
            out = runner.run(who, tool, args, job, post=post)
        except (RunError, SandboxError) as e:
            raise ToolError(str(e)) from None
        if job is not None:
            if news:
                out["human_edits"] = news
                out["human_note"] = ("Claude 가 이 문서를 마지막으로 본 뒤 사용자가 실시간 화면에서 직접 고친 판이 있다. "
                                     "그 내용을 반영해 이어서 작업하고, 필요하면 사용자에게 확인할 것.")
            job.live["claude_seen"] = job.current.v
            sandbox.save_meta(job)
            out["job"] = job.id
            out["current"] = f"v{job.current.v}"
            if out.get("new_version"):
                url = _live_url(who, job)
                out["live_view"] = url
                out["tell_user"] = (f"사용자가 수정을 보고 싶어 하거나 결과를 보여줄 때는 답변 맨 위에 한 줄로 "
                                    f"'실시간으로 보기: {url}' — 이 화면에서 판 비교·되돌리기·직접 고치기·원본 폴더 적용이 된다")
        return out

    def _doc(who: Principal, doc: str, edit: bool = False, version: str | None = None) -> tuple[Job | None, str]:
        """doc = 문서 경로 또는 작업 ID. 문서 폴더의 문서는 뒤에서 조용히 작업(샌드박스 사본)에 붙는다:
        이미 그 문서의 작업이 있으면 그 최신 판을, 없으면 읽기는 원본에서 바로, 편집은 사본을 만들어서."""
        d = (doc or "").strip()
        if re.fullmatch(r"[a-z0-9]{6}", d):
            return _job(who, d), version or "current"
        try:
            root, rel, real = resolve(cfg.roots, d)
        except PathError as e:
            raise ToolError(str(e)) from None
        if not who.can_see(root):
            raise ToolError(f"{who.label} 은(는) 루트 {root!r} 를 볼 수 없다. 볼 수 있는 루트: {who.visible_roots(all_roots)}")
        if not real.is_file() or real.suffix.lower() not in DOC_EXT:
            raise ToolError(f"한글 문서가 아니거나 없다: {doc}")
        try:
            j = sandbox.for_source(who.username, real, f"{root}/{rel}", create=edit or bool(version))
        except SandboxError as e:
            raise ToolError(str(e)) from None
        if j is None:
            return None, f"{root}/{rel}"
        return j, version or "current"

    # ---- 작업 ----------------------------------------------------------------

    @mcp.tool(annotations=ro)
    def hwp_jobs(ctx: Context, job: str | None = None) -> dict[str, Any]:
        """[jobs / 편집 이력] 내가 고치고 있는 문서들(샌드박스 사본, 최근 것부터). job 을 주면 그 판 이력과 부산물 파일."""
        who = _who(ctx)
        if job:
            return _job(who, job).summary()
        return {"jobs": [{"job": j.id, "name": j.name, "source": j.source, "current": f"v{j.current.v}",
                          "updated": j.current.at} for j in sandbox.list(who.username)],
                "keep_days": cfg.keep_days}

    # ---- 조사 ----------------------------------------------------------------

    @mcp.tool(annotations=ro)
    def hwp_info(ctx: Context, doc: str, version: str | None = None) -> dict[str, Any]:
        """[info] 쪽수·구역·문단 수·글꼴·형식·마지막 저장 프로그램."""
        who = _who(ctx); j, ref = _doc(who, doc, version=version)
        return _run(who, "hwp_info", {"path": ref}, j)

    @mcp.tool(annotations=ro)
    def hwp_locate(ctx: Context, doc: str, query: str, context: int = 0, version: str | None = None) -> dict[str, Any]:
        """[search / locate / 어디 있나] 문구가 몇 쪽, 어느 문단·어느 표 칸에 있는지. context=N 이면 앞뒤 N문단도."""
        who = _who(ctx); j, ref = _doc(who, doc, version=version)
        args = {"path": ref, "query": query}
        if context:
            args["context"] = context
        return _run(who, "hwp_search", args, j)

    @mcp.tool(annotations=ro)
    def hwp_tables(ctx: Context, doc: str, table: int | None = None, version: str | None = None) -> dict[str, Any]:
        """[tables / 표 좌표] 표를 병합 셀 보존 격자로 (index·row·col). hwp_set_cell 전에 좌표를 여기서 확인한다."""
        who = _who(ctx); j, ref = _doc(who, doc, version=version)
        notes: list[str] = []

        def pick(res: Any) -> Any:
            if table is None or not isinstance(res, dict):
                return res
            tabs = [t for t in res.get("tables") or [] if t.get("index") == table]
            res["tables"] = tabs
            if any(t.get("rows") == 1 and t.get("cols") == 1 and any(c.get("nested") for c in t.get("cells", []))
                   for t in tabs):
                notes.append("이 표는 1x1 테두리 안에 표들이 중첩된 꼴이다. 칸 편집 도구(hwp_set_cell 등)는 최상위 표만 "
                             "다루므로 중첩 표의 칸은 hwp_replace_text(먼저 dry_run 으로 한 곳인지 확인)로 고친다.")
            return res

        out = _run(who, "hwp_export_tables", {"path": ref}, j, post=pick)
        if notes:
            out["nested_note"] = notes[0]
        return out

    @mcp.tool(annotations=ro)
    def hwp_fields(ctx: Context, doc: str, version: str | None = None) -> dict[str, Any]:
        """[fields / 누름틀] 누름틀(양식 칸) 이름과 현재 값. hwp_fill_fields 전에 본다."""
        who = _who(ctx); j, ref = _doc(who, doc, version=version)
        return _run(who, "hwp_fields", {"path": ref}, j)

    @mcp.tool(annotations=ro)
    def hwp_structure(ctx: Context, doc: str, version: str | None = None) -> dict[str, Any]:
        """[structure / 개요 / 조문] 장·절·조 같은 문서 구조와 제목 목록."""
        who = _who(ctx); j, ref = _doc(who, doc, version=version)
        return _run(who, "hwp_export_structure", {"path": ref}, j)

    @mcp.tool(annotations=ro)
    def hwp_extract(ctx: Context, doc: str, kind: str | None = None, version: str | None = None) -> dict[str, Any]:
        """[extract / 금액 / 날짜] 금액·날짜·수량을 위치와 함께 구조화해서 뽑는다. kind 로 종류를 좁힌다."""
        who = _who(ctx); j, ref = _doc(who, doc, version=version)
        args = {"path": ref}
        if kind:
            args["kind"] = kind
        return _run(who, "hwp_extract_data", args, j)

    @mcp.tool(annotations=ro)
    def hwp_text(ctx: Context, doc: str, page: int | None = None, version: str | None = None) -> dict[str, Any]:
        """[text / 본문] 본문 텍스트. page(0부터)로 한 쪽만. 긴 문서면 결과가 out/ 파일로 떨어진다."""
        who = _who(ctx); j, ref = _doc(who, doc, version=version)
        args = {"path": ref}
        if page is not None:
            args["page"] = page
        return _run(who, "hwp_export_text", args, j)

    # ---- 편집 (새 판) ----------------------------------------------------------

    @mcp.tool(annotations=wr, meta=UI)
    def hwp_fill_fields(ctx: Context, doc: str, data: dict[str, str], dry_run: bool = False) -> dict[str, Any]:
        """[fill / 누름틀 채우기] {"필드이름":"값"} 으로 누름틀을 채워 새 판을 만든다. 같은 이름이 여럿이면 "이름[N]"."""
        who = _who(ctx); j, _ = _doc(who, doc, edit=True)
        return _run(who, "hwp_fill_fields", {"path": "current", "data": data, **({"dryRun": True} if dry_run else {})}, j)

    @mcp.tool(annotations=wr, meta=UI)
    def hwp_set_cell(ctx: Context, doc: str, table: int, row: int, col: int, text: str, dry_run: bool = False) -> dict[str, Any]:
        """[set cell / 표 칸 쓰기] 표 좌표(hwp_tables 의 index·row·col)의 칸 값을 바꿔 새 판을 만든다. 빈 문자열이면 비운다."""
        who = _who(ctx); j, _ = _doc(who, doc, edit=True)
        args = {"path": "current", "table": table, "row": row, "col": col, "text": text}
        if dry_run:
            args["dryRun"] = True
        return _run(who, "hwp_set_cell", args, j)

    @mcp.tool(annotations=wr, meta=UI)
    def hwp_replace_text(ctx: Context, doc: str, find: str, replace: str, dry_run: bool = False) -> dict[str, Any]:
        """[replace / 바꾸기] 문구를 전부 바꿔 새 판을 만든다 (replace 가 빈 문자열이면 삭제). 먼저 dry_run 으로 몇 곳인지 볼 것.
        k번째 하나만 바꾸려면 hwp_run("hwp_replace_text", …) 대신 hwp_catalog 로 occurrence 인자를 확인."""
        who = _who(ctx); j, _ = _doc(who, doc, edit=True)
        args = {"path": "current", "find": find, "replace": replace}
        if dry_run:
            args["dryRun"] = True
        return _run(who, "hwp_replace_text", args, j)

    WRITING_RULES = """공문서·보고서 작성 규범 (hwp_new 로 만들 때)
- 문체: 명사형·개조식으로 끝맺는다 (~임, ~함, ~중, ~ 필요, ~ 추진). 한 항목은 1~2줄. 수식어·접속사는 덜어낸다.
- 위계: □(l1) → ❍(l2) → -(l3) → ·(l4). 상위 항목이 결론, 하위가 근거. 같은 위계는 같은 문형으로.
- 보고서: 표지(rule·title·subtitle·rule·date·sign) → 목차(toc, 쪽은 비워 두면 자동) → 요약 상자(box) →
  장(h1 + num "Ⅰ" + page_restart 1 은 첫 장에만) → 절(h2 "1.") → 항목. 장마다 새 쪽이 필요할 때만 page_break.
- 기안문: 기관명 → 수신·(경유)·제목 → "1. 관련:" → "2. 위 호와 관련하여 …" → 가. 나. → 붙임 → 끝. → 발신명의 → 결재·시행·주소.
  끝맺음은 마지막 줄 끝에 두 칸 띄우고 "끝." (붙임이 있으면 붙임 줄 끝에).
- 표: 머리행은 가운데·굵게·음영(기본), 수치 열은 오른쪽(align "right"), 단위는 표 위 오른쪽에 "(단위: 백만원)"(style caption),
  출처·단서는 표 아래 ※(note). 일정표는 기간 칸을 fill 로 칠한다.
- 강조는 아껴서: 소제목 런은 파란 굵게, 꼭 읽혀야 할 구절만 굵게+밑줄. 색은 남색·파랑 계열 한두 가지.
- 날짜는 "2026. 10. 1.", 기간은 "'26.11. ~ '27.4.", 금액은 천 단위 쉼표.
- 메모보고(견본 "메모보고", 사용자가 메모보고·한 장 보고를 원할 때): 글꼴은 제목 HY헤드라인M 17pt, 날짜·보고자 줄 맑은 고딕 12pt
  "< ’26. 10. 19. (월). 실·국·과장 >", ◇ 핵심 요약 함초롬바탕 14pt, □ 중제목 HY헤드라인M 15pt, ㅇ·- 내용 한컴 윤고딕 230 15pt,
  * 부연(통계·예시) 맑은 고딕 12pt. 굵게·스타일 금지(□ ㅇ - 모두), 장평 100% 고정, 자간은 건드리지 않는다, 줄간격 160%.
  기호 앞 칸: □ 0칸, ㅇ(한글 이응) 1칸, - 3칸, * 4칸. 여백 좌우 20mm·위아래 10mm. 한 쪽 20줄 안팎.
  보고 유형 표에서 하나만 ( V ): 단순 보고(보고 배경→보고 내용→향후 계획) · 결정 필요(보고 배경→대안별 분석→결정 사항: 1안 채택을 건의드림)
  · 건의 사항(보고 배경→건의 사항: 무엇을 건의드림→향후 계획). 바라는 바를 서면만으로 알 수 있게 분명히 쓰고, 자간·줄간격을 줄여
  한 장에 우겨넣지 말고 다음 쪽으로 넘긴다. 결재자 의견란이 안 들어가면 다음 쪽으로. 표 숫자는 오른쪽 정렬, 단위는 읽기 쉽게(4,234억원).
- 수치·근거를 지어냈다면 문서에 쓰지 말고 사용자에게 따로 알린다.
- 만든 뒤 hwp_preview 로 표지·목차·첫 장·표가 있는 쪽을 보고, 반 넘게 빈 쪽·넘친 표·어색한 줄바꿈을 고쳐 다시 만든다."""

    def _templates() -> dict[str, dict]:
        out = {}
        for f in sorted((resources.files("hwp_cowork") / "templates").iterdir(), key=lambda x: x.name):
            if f.name.endswith(".json"):
                out[f.name[:-5]] = json.loads(f.read_text(encoding="utf-8"))
        return out

    @mcp.tool(annotations=ro)
    def hwp_templates(ctx: Context, name: str | None = None) -> dict[str, Any]:
        """[templates / 견본 / 양식] hwp_new 에 넣을 **견본 명세**와 공문서 작성 규범. 새 문서를 만들기 전에 먼저 본다.
        name 없이 부르면 견본 목록과 규범, name 을 주면 그 견본 spec 전체 — 〔〕 안을 바꾸고 블록을 늘리거나 줄여
        그대로 hwp_new(spec=…) 에 넣는다."""
        _who(ctx)
        ts = _templates()
        if not name:
            return {"templates": [{"name": k, "about": v.get("_about", "")} for k, v in ts.items()],
                    "rules": WRITING_RULES, "next": "hwp_templates(name=…) 로 견본 spec 을 받는다."}
        if name not in ts:
            raise ToolError(f"그런 견본이 없다: {name!r}. 있는 것: {sorted(ts)}")
        spec = {k: v for k, v in ts[name].items() if k != "_about"}
        return {"name": name, "about": ts[name].get("_about", ""), "spec": spec, "rules": WRITING_RULES}

    @mcp.tool(annotations=wr, meta=UI)
    def hwp_new(ctx: Context, name: str, spec: dict[str, Any], format: str = "hwpx", ingest: bool = False) -> dict[str, Any]:
        """[new / 새 문서 / 보고서·공문서 작성] 원본 없이 **서식까지 갖춘** 한글 문서를 만든다 → 새 작업의 v0.
        글꼴·크기·굵기·색·밑줄·정렬·내어쓰기·줄간격, 표(열 너비·머리행 음영·병합·칸 색·가로선만), 색 띠, 테두리 상자,
        점선 목차, 머리말·쪽 번호까지 명세 한 번으로. 만든 뒤에는 job 을 doc 로 주고 다른 편집 도구로 다듬는다.
        format="hwp" 면 HWP5(.hwp)로. 사용자가 폴더에 둘 곳은 실시간 화면의 "폴더에 저장" 에서 고른다.

        spec = {"page": {"margin":[위,아래,왼,오른 mm], "header":"머리말" 또는 {"p":…, "align", "size", "rule":"#색"(아래 선)},
                         "page_number":"- n -"|false, "cover":true(첫 쪽 머리말·쪽번호 감춤, 앞 블록이 rule 이면 자동)},
                "fonts": {"body":"휴먼명조", "head":"HY헤드라인M", "table":"맑은 고딕"}, "base": {"size":13, "line":160},
                "blocks": [블록…]}
        블록 (단위: 글자 pt · 길이 mm · 줄간격 %):
          {"p": "글" 또는 [{"t":"글","b":true,"u":true,"i":true,"color":"#1F4E9A","size":15,"font":"head","shade":"#FFFF00"}…],
           "style": title|subtitle|h1|h2|h3|l1(□)|l2(❍)|l3(-)|l4(·)|note(※)|body|center|right|date|sign|caption,
           그 밖에 size bold italic underline color font spacing(자간) align(left|center|right|justify|distribute)
           indent hanging before after line page_break 로 덮어쓴다. 문단 장식: "fill"(배경), "bar"(왼쪽 색 막대),
           "line_below"/"line_above"(색 선). "num":"Ⅰ" = 남색 상자 안 흰 장 번호(num_fill 로 색).
           "page_restart":1 = 이 문단 쪽부터 쪽 번호 1 (표지·목차 빼고 본문 1쪽). 줄 나눔은 어절 단위("wrap":"char" 면 글자)}
          {"table": [[칸…]…], "widths":[비율…], "header":1, "head_fill":"#D9D9D9", "align":["center","left",…],
           "size":11, "col_size":[열별 pt…], "merge":[[행,열,행수,열수]…], "fill":{"행,열":"#색"},
           "border":"grid|rows|none", "outer":"thick|normal", "head_line":"double|thick"(머리행 아래 선)}
             칸 = "글" 또는 {"t"|"p", "fill", "align", "b", "color", "size", "valign", "line_r":"double|dash|none"(한 변만, line_l·line_t·line_b 도)}, 표의 "pad":[왼,오른,위,아래 mm]·"heights":[행별 mm]
          {"rule":"#4472C4", "thick":2.5}  가로 색 띠 (표지 제목 위아래)
          {"box": "글" 또는 [블록…], "fill":"#EEF3FA", "line":"#4472C4", "border":"solid|double|dash|dot"}  요약 상자
          {"toc": ["Ⅰ. 추진 배경", "1. 정책 동향", …]}   점선 채움 목차 — 쪽을 안 주면 실제 쪽을 찾아 자동으로 채운다
            (제목 글이 본문 제목과 같아야 찾는다. ["제목","3"] 처럼 손으로 줄 수도 있다)
          {"space": pt}  ·  {"page_break": true}
        공문서 관례: 표지(rule·title·subtitle·rule·space·date·sign) → page_break → 목차 → 요약 box → h1 장마다
        l1(□) → l2(❍) → l3(-) 위계, 표 아래 note(※). 덮어쓴 색·크기는 꼭 필요한 곳에만.
        만든 뒤에는 hwp_preview 로 쪽 이미지를 직접 보고 어색한 곳을 고친다 (링크를 못 여는 환경에서도 된다).
        처음 만들 때는 hwp_templates 의 견본(보고서·기안문·메모보고)과 작성 규범을 먼저 받아 그 위에서 쓴다.
        ingest=true 면 spec 을 rhwp ingest 문서(build-from-ingest)로 그대로 넘긴다."""
        who = _who(ctx)
        base = (name or "").strip()
        if not base or "/" in base or "\\" in base or base.startswith("."):
            raise ToolError(f"이름이 이상하다: {name!r}")
        for x in (".hwpx", ".hwp", ".hml"):
            if base.lower().endswith(x):
                base = base[: -len(x)]
        if format not in ("hwpx", "hwp"):
            raise ToolError("format 은 hwpx 또는 hwp")
        try:
            work = Path(tempfile.mkdtemp(prefix=".new-", dir=sandbox.user_dir(who.username)))
        except SandboxError as e:
            raise ToolError(str(e)) from None
        tool = "hwp_build_from_ingest" if ingest else "hwp_new"
        try:
            if ingest:
                produced, info = runner.create("hwp_build_from_ingest", spec, work)
            else:
                produced, info = runner.compose(spec, work, format)
            j = sandbox.create(who.username, produced, "", base + produced.suffix)
        except (RunError, SandboxError) as e:
            raise ToolError(str(e)) from None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        j.versions[0].by, j.versions[0].note = tool, "새로 만듦"
        sandbox.save_meta(j)
        url = _live_url(who, j)
        return {"job": j.id, "name": j.name, "current": "v0", "result": info, "live_view": url,
                "next": "hwp_preview(doc=job, pages=\"1-3\") 로 쪽을 눈으로 확인하고, 고칠 곳은 spec 을 고쳐 다시 만들거나 "
                        "hwp_replace_text·hwp_set_cell 로 다듬는다. 폴더 저장은 사용자가 실시간 화면에서 "
                        "폴더를 골라 하거나, 사용자가 폴더를 말하면 hwp_save_copy(doc=job, folder=…).",
                "tell_user": f"답변 맨 위에 한 줄: 실시간으로 보기: {url}"}

    @mcp.tool(annotations=ro)
    def hwp_preview(ctx: Context, doc: str, pages: str = "1", version: str | None = None) -> list:
        """[preview / 미리보기 / 눈으로 확인] 쪽을 **이미지로** 돌려준다 — 만들거나 고친 결과를 직접 보고 확인한다.
        pages 는 1부터: "1", "2-4", "1,3,5" (한 번에 6쪽까지). 링크를 열 수 없는 환경에서도 된다.
        글자가 어절 중간에서 끊기는지, 표가 넘치는지, 쪽이 비는지, 표지·목차 모양을 여기서 본다."""
        who = _who(ctx); j, ref = _doc(who, doc, version=version)
        want: list[int] = []
        for part in str(pages).replace(" ", "").split(","):
            if not part:
                continue
            try:
                a, _, b = part.partition("-")
                lo, hi = int(a), int(b or a)
            except ValueError:
                raise ToolError(f"pages 형식: \"1\", \"2-4\", \"1,3\" — 받은 것 {pages!r}") from None
            want += list(range(lo, hi + 1))
        want = [p for i, p in enumerate(want) if p >= 1 and p not in want[:i]]
        if not want or len(want) > 6:
            raise ToolError("한 번에 1~6쪽까지.")
        try:
            if j is None:
                path = resolve(cfg.roots, ref)[2]
            else:
                path = j.path(None if ref == "current" else int(str(ref).lstrip("v")))
            imgs = runner.preview(path, [p - 1 for p in want])
        except (RunError, SandboxError, PathError, ValueError) as e:
            raise ToolError(str(e)) from None
        out: list = [f"{len(imgs)}쪽 미리보기: " + ", ".join(f"{p + 1}쪽" for p, _ in imgs)]
        out += [Image(data=png, format="png") for _, png in imgs]
        return out

    # ---- 확인 ----------------------------------------------------------------

    @mcp.tool(annotations=ro)
    def hwp_diff(ctx: Context, doc: str, a: str = "v0", b: str = "current") -> dict[str, Any]:
        """[diff / 비교 / 검증] 두 판을 구조(IR) 수준에서 비교한다 — 기본은 원본(v0) 대 지금. 의도한 곳만 바뀌었는지 확인."""
        who = _who(ctx); j, _ = _doc(who, doc, edit=True)
        out = _run(who, "hwp_ir_diff", {"a": a, "b": b}, j)
        # rhwp 의 구조 비교는 바뀐 곳의 종류·개수만 준다 — 무엇이 무엇으로 바뀌었는지는 본문 비교로 붙인다
        try:
            td = _text_diff(who.username, j, a if a != "current" else f"v{j.current.v}",
                            b if b != "current" else f"v{j.current.v}", limit=30)
            out["changes"] = td["changes"]
            out["change_count"] = td["count"]
        except (RunError, SandboxError, ToolError, OSError, ValueError) as e:
            out["changes_error"] = str(e)[:300]
        return out

    @mcp.tool(annotations=wr, meta=UI)
    def hwp_undo(ctx: Context, doc: str) -> dict[str, Any]:
        """[undo / 되돌리기] 마지막 판을 지운다 (v0 원본 사본은 지우지 않는다)."""
        who = _who(ctx); j, _ = _doc(who, doc, edit=True)
        try:
            gone = sandbox.undo(j)
        except SandboxError as e:
            raise ToolError(str(e)) from None
        return {"job": j.id, "removed": f"v{gone.v}", "current": f"v{j.current.v}"}

    # ---- 전체 기능 --------------------------------------------------------------

    @mcp.tool(annotations=ro)
    def hwp_catalog(ctx: Context, query: str | None = None) -> dict[str, Any]:
        """[catalog / 기능 찾기] rhwp 전체 기능(150여 개)에서 찾는다. query 없이 부르면 이름·한 줄 목록,
        query("표 병합", "각주", "PDF", "도장", "개인정보")를 주면 설명과 인자까지. 찾은 이름을 hwp_run 에 쓴다."""
        _who(ctx)
        items = manifest.search(query)
        return {"count": len(items), "tools": items,
                "how": "hwp_run(tool=이름, args={...}, doc=문서경로 또는 작업ID). 문서 인자는 생략하면 doc 의 최신 판. 출력 인자는 이름만."}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
              meta=UI)
    def hwp_run(ctx: Context, tool: str, args: dict[str, Any] | None = None, doc: str | None = None) -> dict[str, Any]:
        """[run / 실행] hwp_catalog 로 찾은 rhwp 기능을 실행한다. 편집 기능이면 새 판이 생기고, 파일을 만드는 기능이면 out/ 에 남는다.

        doc 는 문서 경로 또는 작업 ID — 편집 기능이면 뒤에서 사본을 만들어 새 판이 생긴다. args 의 문서 인자는 생략하면 doc 의
        최신 판, 또는 "v2" · "out/파일" · 문서 경로(읽기만). 출력 인자는 파일 이름만.
        doc 없이는 읽기 기능만 — 문서 경로·폴더를 args 에 직접 준다 (예: hwp_batch_search {paths:["docs/2027"], query:"위임전결"}).
        """
        who = _who(ctx)
        t = manifest.tools.get(tool)
        j = None
        if doc:
            j, ref = _doc(who, doc, edit=bool(t and not t.read_only))
            args = dict(args or {})
            if t and "path" in t.props and "path" not in args:
                args["path"] = ref
        return _run(who, tool, args or {}, j)

    # ---- 반출 · 실시간 화면 ------------------------------------------------------

    LIVE_TTL = 12 * 3600

    def _pick(j: Job, version: str | None, file: str | None):
        if file:
            p = j.out / file.removeprefix("out/")
            if "/" in file.removeprefix("out/") or not p.is_file():
                raise ToolError(f"작업 {j.id} 의 out/ 에 없다: {file}. 있는 것: {j.summary()['files']}")
            return p
        try:
            return j.path(int(version[1:]) if version and version.startswith("v") else None)
        except (SandboxError, ValueError) as e:
            raise ToolError(str(e)) from None

    def _live_url(who: Principal, j: Job) -> str:
        """작업마다 실시간 화면 링크 하나를 재사용한다 (12시간, 남은 시간이 1시간 밑이면 새로)."""
        now = time.time()
        if j.live.get("url") and j.live.get("exp", 0) - now > 3600 and j.live.get("user") == who.username:
            return j.live["url"]
        url, exp = signer.make("live", f"{who.username}/{j.id}", who.label, rw=who.write, ttl=LIVE_TTL)
        j.live = {"url": url, "exp": exp, "user": who.username}
        sandbox.save_meta(j)
        return url

    def _local_put(username: str, target: str, base: str, data: bytes) -> dict:
        """쓰기 허용 루트(HWPCOWORK_WRITABLE_ROOTS)에 직접 사본을 쓴다. 같은 이름이 있으면 `이름_vN` 으로 비켜 쓴다."""
        root, rel, real = resolve(cfg.roots, target)
        if root not in cfg.writable_roots:
            raise ToolError(f"루트 {root!r} 는 쓰기 허용이 아니다 (HWPCOWORK_WRITABLE_ROOTS).")
        if not real.is_dir():
            raise ToolError(f"폴더가 없다: {target}")
        stem, dot, ext = base.rpartition(".")
        if not dot:
            stem, ext = base, ""
        out, n = real / base, 1
        while out.exists():
            out = real / f"{stem}_v{n}{dot}{ext}"
            n += 1
        tmp = out.with_name(f".{out.name}.part")
        tmp.write_bytes(data)
        tmp.replace(out)
        return {"path": f"{root}/{rel + '/' if rel else ''}{out.name}", "bytes": len(data)}

    def _nas_put(username: str, j: Job, p, mode: str, folder: str | None = None, name: str | None = None) -> dict:
        """원본 폴더(없으면 folder)에 사본을 넣는다 — 쓰기 허용 루트면 직접, 아니면 외부 저장 엔드포인트
        (HWPCOWORK_NAS_SAVE_URL)로. 같은 이름이 있으면 `이름_vN` 으로 비켜 쓴다. 원본을 덮어쓰는 길은 두지 않는다."""
        target = folder or (j.source.rsplit("/", 1)[0] if "/" in j.source else None)
        if not target:
            raise ToolError("저장할 폴더를 folder 로 줄 것.")
        base = name or j.name
        if "/" in base or "\\" in base or not base.strip() or base.startswith("."):
            raise ToolError(f"이름이 이상하다: {base!r}")
        if not any(base.lower().endswith(x) for x in DOC_EXT):
            base += p.suffix
        target_root = target.strip("/").split("/", 1)[0]
        # 저장할 폴더의 루트를 그 사람이 볼 수 있어야 한다 — 읽을 수 없는 루트에 쓰는 길을 막는다
        if not _owner(username).can_see(target_root):
            raise ToolError(f"루트 {target_root!r} 에는 저장할 수 없다 (볼 수 있는 루트가 아니다).")
        if target_root in cfg.writable_roots:
            try:
                body = _local_put(username, target, base, p.read_bytes())
            except PathError as e:
                raise ToolError(str(e)) from None
        elif cfg.nas_save_url and cfg.nas_internal_key:
            params = {"user": username, "folder": target, "name": base}
            try:
                r = httpx.post(cfg.nas_save_url, params=params, content=p.read_bytes(),
                               headers={"X-Internal-Key": cfg.nas_internal_key}, timeout=120)
            except httpx.HTTPError as e:
                raise ToolError(f"저장 서버에 닿지 못했다: {e}") from None
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text[:300]}
            if r.status_code != 200:
                raise ToolError(f"저장 실패: {body.get('error')}")
        elif cfg.writable_roots:
            raise ToolError(f"{target_root!r} 는 읽기 전용 폴더다 — 쓸 수 있는 곳: {sorted(cfg.writable_roots)}. "
                            "아니면 hwp_download_link 로 받을 것.")
        else:
            raise ToolError("이 서버는 읽기 전용이다 (HWPCOWORK_WRITABLE_ROOTS 없음) — hwp_download_link 로 받을 것.")
        log.info("save %s %s: %s %s → %s", mode, username, j.id, p.name, body.get("path"))
        j.live.setdefault("applied", []).append({"v": p.stem, "mode": mode, "path": body.get("path"),
                                                 "at": datetime.now().isoformat(timespec="seconds")})
        sandbox.save_meta(j)
        return body

    @mcp.tool(annotations=ro, meta=UI)
    def hwp_view_link(ctx: Context, doc: str, version: str | None = None) -> dict[str, Any]:
        """[view / live / 보여줘 / 실시간] 문서를 **실시간 화면**으로 여는 링크 (12시간). Claude 가 고칠 때마다 화면이 바로
        바뀌고, 판 이력·비교·되돌리기, 사람이 직접 고치기(자동으로 새 판), 폴더에 최종본 적용까지 그 화면에서 된다.
        사용자에게는 답변 **맨 위에** 한 줄로 "실시간으로 보기: <url>" 을 준다."""
        who = _who(ctx); j, _ = _doc(who, doc, edit=True)
        url = _live_url(who, j)
        if version:
            url += f"?v={version}"
        return {"job": j.id, "url": url, "current": f"v{j.current.v}",
                "tell_user": f"답변 맨 위에 한 줄: 실시간으로 보기: {url}"}

    @mcp.tool(annotations=ro)
    def hwp_download_link(ctx: Context, doc: str, version: str | None = None, file: str | None = None) -> dict[str, Any]:
        """[download / 받기] 판(version) 또는 부산물(file: out/이름 — PDF·CSV 등)을 내려받는 짧은 링크 (30분)."""
        who = _who(ctx); j, _ = _doc(who, doc, edit=True)
        p = _pick(j, version, file)
        rel = f"out/{p.name}" if p.parent == j.out else p.name
        url, exp = signer.make("dl", f"{who.username}/{j.id}/{rel}", who.label)
        return {"job": j.id, "file": rel, "url": url, "expires_at": datetime.fromtimestamp(exp).strftime("%H:%M")}

    @mcp.tool(annotations=wr)
    def hwp_save_copy(ctx: Context, doc: str, version: str | None = None, folder: str | None = None,
                        name: str | None = None) -> dict[str, Any]:
        """[save / 폴더에 저장] 판을 폴더에 **사본**으로 저장한다 — 원본 옆에 `원래이름_vN.hwp` (N 은 폴더에 있는 다음 번호).
        원본 자체를 바꾸는 일은 사용자가 실시간 화면의 "폴더에 적용" 에서 직접 고른다. 폴더 쓰기 권한이 있는 사람만."""
        who = _who(ctx); j, _ = _doc(who, doc, edit=True)
        if not who.write:
            raise ToolError(f"{who.label} 은(는) 폴더 쓰기 권한이 없다 — hwp_download_link 로 받을 것.")
        p = _pick(j, version, None)
        body = _nas_put(who.username, j, p, "copy", folder, name)
        return {"job": j.id, "version": p.stem, "saved_to": body.get("path"), "bytes": body.get("bytes"),
                "note": "원본 폴더에 사본으로 저장했다."}

    # ---- 채팅 안 화면 (MCP Apps) ----------------------------------------------------

    app_html = (resources.files("hwp_cowork") / "static" / "app.html").read_text(encoding="utf-8")

    @mcp.resource(APP_URI, name="hwp_viewer", title="한글 미리보기", mime_type=APP_MIME_TYPE,
                  meta={"ui": {"csp": {"connectDomains": sorted({cfg.upload_base, cfg.public_base})}, "prefersBorder": True}})
    def hwp_viewer_html() -> str:
        return app_html

    def _app_info(who: Principal, j: Job | None) -> dict:
        if j is None:
            return {"job": None, "busy": None, "human": False, "rev": ""}
        cur = j.current
        return {"job": j.id, "name": j.name, "current": f"v{cur.v}", "busy": Sandbox.busy_state(j),
                "human": bool((h := _human(j)) and h.get("dirty")), "rev": _rev(j), "changed_pages": cur.pages[:10]}

    @mcp.tool(annotations=ro, meta=APP_ONLY)
    def hwp_app_page(ctx: Context, doc: str, page: int = 1) -> list:
        """[앱 화면 전용] 채팅 안 미리보기가 쪽 하나를 그릴 때 부른다. 첫 줄 JSON(작업·판·쪽 수), 다음 PNG."""
        who = _who(ctx); j, ref = _doc(who, doc)
        try:
            path = resolve(cfg.roots, ref)[2] if j is None else j.path()
            png, total = runner.page_png(path, max(int(page), 1) - 1)
        except (RunError, SandboxError, PathError, ValueError) as e:
            raise ToolError(str(e)) from None
        info = {**_app_info(who, j), "page": min(max(int(page), 1), total), "pages": total}
        if j is None:
            info["name"] = ref.rsplit("/", 1)[-1]
        return [json.dumps(info, ensure_ascii=False), Image(data=png, format="png")]

    @mcp.tool(annotations=ro, meta=APP_ONLY)
    def hwp_app_state(ctx: Context, job: str) -> dict[str, Any]:
        """[앱 화면 전용] 작업이 바뀌었는지 (3초마다) — rev 가 달라지면 화면이 다시 그린다."""
        who = _who(ctx)
        return _app_info(who, _job(who, job))

    @mcp.tool(annotations=ro, meta=APP_ONLY)
    def hwp_app_editor(ctx: Context, job: str) -> dict[str, Any]:
        """[앱 화면 전용] "편집기" 버튼 — 실시간 편집 화면 링크."""
        who = _who(ctx)
        return {"url": _live_url(who, _job(who, job))}

    def _warm(path: Path) -> None:
        try:
            runner.page_png(path, 0)
        except Exception:  # noqa: BLE001 — 미리 그리기는 실패해도 그만
            pass

    UP_PROGRESS: dict[str, tuple[int, int, float]] = {}      # 올리기 코드 → (받은 바이트, 전체, 시각)

    @mcp.tool(annotations=ro, meta=APP_ONLY)
    def hwp_app_uploaded(ctx: Context, upload_url: str) -> dict[str, Any]:
        """[앱 화면 전용] 이 올리기 주소로 들어온 가장 최근 문서 (코드 실행의 curl 로 올라왔는지 화면이 2초마다 본다)."""
        who = _who(ctx)
        code = upload_url.rstrip("/").rsplit("/", 1)[-1]
        hits = [j for j in sandbox.list(who.username) if j.live.get("upload_code") == code]
        prog = UP_PROGRESS.get(code)
        receiving = {"got": prog[0], "total": prog[1]} if prog and time.time() - prog[2] < 30 else None
        if not hits:
            return {"job": None, "receiving": receiving}
        j = max(hits, key=lambda x: x.created)
        return {"job": j.id, "name": j.name, "count": len(hits), "receiving": receiving}

    @mcp.tool(annotations=ro, meta=UI)        # 링크만 만든다 — 승인 필요(쓰기)로 분류되면 claude.ai 가 매번 물어 느려진다
    def hwp_upload_link(ctx: Context) -> dict[str, Any]:
        """[upload / 올리기 / 첨부한 한글 파일 열기] 사용자가 **채팅에 첨부한** 한글 파일(.hwp .hwpx .hml)을 이 서버로 올리는 주소.
        폴더를 거치지 않고 바로 새 작업(job)이 된다 — 폴더 쓰기 권한이 없어도 된다.

        첨부 파일은 코드 실행 환경의 첨부 폴더에 있다 (통합 Claude: /root/.claude/uploads/<대화>/<이름>, 예전: /mnt/user-data/uploads/). 코드 실행(bash)으로:
            curl -sS -F "file=@<첨부 파일 경로>" <upload_url>
        응답 JSON 의 job 을 doc 로 다른 hwp 도구에 쓴다. 주소 하나로 30분 동안 여러 파일을 올릴 수 있다.
        5xx 면 같은 curl 을 한 번만 다시. 올린 뒤 내용은 hwp 도구로 읽는다 — 코드 실행으로 압축을 풀어 읽지 않는다 (최후의 수단).
        첨부 파일 이름의 한글이 밑줄로 바뀌어 있으면(claude.ai 가 바꾼다) 사용자가 말한 원래 이름을 `-F "name=원래이름.hwpx"` 로 같이 준다.
        채팅 안 화면이 있는 곳에서는 이 도구의 화면이 올라온 문서의 1쪽을 저절로 보여 준다 — 사용자가 "보여줘"만 했다면
        hwp_preview 를 또 부르지 말고 짧게 답한다 (Claude 가 직접 내용을 봐야 할 때만 hwp_preview·hwp_text).
        curl 이 거부되면(권한·보안 검사 거부, 허용 도메인 아님, 연결 거부, 코드 실행 없음) **다시 시도하거나 다른 방법으로
        보내려 하지 않는다**. 이 도구 결과의 채팅 안 칸이 사용자가 직접 올리는 길이다 — 한 줄만 말하고 멈춘다:
        "위 칸에 파일을 끌어다 놓아 주세요." (화면이 없는 곳이면 upload_url 을 주고 "이 주소를 열어 파일을 놓아 주세요.")
        사용자가 놓으면 화면이 미리보기로 바뀌고 대화에 job 이 들어온다."""
        who = _who(ctx)
        url, exp = signer.make("hu", who.username, who.label, base=cfg.upload_base)
        return {"upload_url": url, "expires_at": datetime.fromtimestamp(exp).strftime("%H:%M"),
                "curl": f'curl -sS -F "file=@<첨부 파일 경로>" {url}',
                "next": "응답의 job 을 doc 로 쓴다. 채팅 안 화면이 1쪽을 바로 보여 주므로 '보여줘'면 hwp_preview 는 생략."}

    # ---- 링크 라우트 --------------------------------------------------------------

    from starlette.requests import Request
    from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

    static_root = resources.files("hwp_cowork") / "static"

    sso = (WebSSO(registry.verifier.cfg, cfg.public_base, registry.principals, cfg.data_dir, "hwp_sso")
           if registry is not None and registry.verifier is not None else None)   # 없으면 로컬·테스트 — 코드만으로 연다
    throttle = Throttle()
    if sso:
        sso.register(mcp)

    def _live(request: Request, html_page: bool = False):
        """링크 코드 + 링크 주인 계정의 SSO 로그인 쿠키가 둘 다 있어야 연다."""
        if throttle.blocked(request):
            return None, JSONResponse({"error": "잠시 뒤 다시 시도할 것"}, status_code=429)
        try:
            link = signer.verify("live", request.path_params["token"])
            user, jid = link.path.split("/", 1)
            j = sandbox.get(user, jid)
        except (LinkError, SandboxError, ValueError) as e:
            throttle.fail(request)
            return None, JSONResponse({"error": str(e)}, status_code=403)
        if sso and (deny := sso.gate(request, user, html_page=html_page)):
            return None, deny
        return (link, user, j), None

    def _state(j: Job, rw: bool) -> dict:
        s = j.summary()
        s.update(busy=Sandbox.busy_state(j), can_apply=rw and bool(cfg.nas_save_url or cfg.writable_roots), rev=_rev(j),
                 ext=j.current.file.rsplit(".", 1)[-1],
                 applied=j.live.get("applied", []), size=j.path().stat().st_size)
        return s

    def _rev(j: Job) -> str:
        try:
            m = (j.dir / "job.json").stat().st_mtime
        except OSError:
            m = 0
        return f"{m:.3f}|{Sandbox.busy_state(j) or ''}"

    def _ver_path(j: Job, v: str | None):
        try:
            return j.path(int(v.lstrip("v")) if v else None)
        except (SandboxError, ValueError) as e:
            raise LinkError(str(e)) from None

    @mcp.custom_route("/static/{name:path}", methods=["GET"])
    async def static(request: Request):
        name = request.path_params["name"]
        f = static_root / name
        if ".." in name or not f.is_file() or not name.endswith((".js", ".css")):
            return JSONResponse({"error": "없다"}, status_code=404)
        return FileResponse(str(f), media_type="text/javascript" if name.endswith(".js") else "text/css",
                            headers={"Cache-Control": "public, max-age=3600"})

    live_html = (static_root / "live.html").read_text(encoding="utf-8")
    studio_http = httpx.AsyncClient(timeout=60) if cfg.studio_upstream else None

    @mcp.custom_route("/studio/{name:path}", methods=["GET"])
    async def studio_proxy(request: Request):
        """rhwp-studio 를 이 서버 도메인에서 낸다 — HWPCOWORK_STUDIO_DIR(빌드 산출물) 또는 HWPCOWORK_STUDIO_UPSTREAM 중계.
        실시간 화면과 같은 도메인이어야 studio 설정(자동복구 끄기)을 화면이 넣을 수 있다. 공개 정적 자산."""
        name = request.path_params["name"]
        if ".." in name:
            return JSONResponse({"error": "bad path"}, status_code=400)
        if cfg.studio_dir is not None:
            f = (cfg.studio_dir / (name or "index.html")).resolve()
            if f.is_dir():
                f = f / "index.html"
            if not f.is_relative_to(cfg.studio_dir) or not f.is_file():
                return JSONResponse({"error": "없다"}, status_code=404)
            return FileResponse(str(f), headers={"Cache-Control": "public, max-age=3600"})
        if studio_http is None:
            return JSONResponse({"error": "studio 가 설정되지 않았다 (HWPCOWORK_STUDIO_DIR)"}, status_code=404)
        try:
            r = await studio_http.get(cfg.studio_upstream + name)
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"studio 에 닿지 못했다: {e}"}, status_code=502)
        keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control", "etag", "last-modified")}
        from starlette.responses import Response
        return Response(r.content, status_code=r.status_code, headers=keep)

    @mcp.custom_route("/live/{token}", methods=["GET"])
    async def live_page(request: Request):
        ok, err = _live(request, html_page=True)
        if err is not None and (err.status_code != 403 or "text/html" in err.headers.get("content-type", "")):
            return err          # 로그인 화면으로 보내기·다른 계정 안내·429
        if err:
            return HTMLResponse("<meta charset=utf-8><body style='font-family:system-ui;padding:2rem'>"
                                "<h3>링크가 만료됐어요</h3><p>Claude 에게 \"실시간으로 보여줘\" 라고 하면 새 링크를 줘요.</p>", 403)
        link, _u, j = ok
        html = live_html.replace("__STUDIO__", cfg.studio_url).replace("__TITLE__", _esc(j.name))
        return HTMLResponse(html.replace("<head>", '<head><base href="/">', 1), headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/live/{token}/state", methods=["GET"])
    async def live_state(request: Request):
        ok, err = _live(request)
        if err:
            return err
        link, _u, j = ok
        return JSONResponse(_state(j, link.rw))

    @mcp.custom_route("/live/{token}/events", methods=["GET"])
    async def live_events(request: Request):
        """판이 생기거나 Claude 가 편집을 시작·끝낼 때 state 를 밀어준다 (SSE). 1초마다 job.json·편집 표시를 본다."""
        ok, err = _live(request)
        if err:
            return err
        link, user, j0 = ok

        async def gen():
            last, idle = None, 0
            while not await request.is_disconnected():
                try:
                    j = sandbox.get(user, j0.id)
                except SandboxError:
                    yield "event: gone\ndata: {}\n\n"
                    return
                rev = _rev(j)
                if rev != last:
                    last, idle = rev, 0
                    yield f"event: state\ndata: {json.dumps(_state(j, link.rw), ensure_ascii=False)}\n\n"
                else:
                    idle += 1
                    if idle % 15 == 0:
                        yield ": ping\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @mcp.custom_route("/live/{token}/file", methods=["GET"])
    async def live_file(request: Request):
        ok, err = _live(request)
        if err:
            return err
        _l, _u, j = ok
        try:
            p = _ver_path(j, request.query_params.get("v"))
        except LinkError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        if request.query_params.get("dl"):
            return FileResponse(str(p), filename=f"{j.name.rsplit('.', 1)[0]}_{p.stem}{p.suffix}")
        return FileResponse(str(p), media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/live/{token}/save", methods=["POST"])
    async def live_save(request: Request):
        """에디터에서 사람이 고친 것을 새 판으로. auto=1 이면 연속 자동 저장을 한 판으로 묶는다.
        base 는 에디터가 불러온 판 — 그 사이 Claude 가 새 판을 만들었으면 409 (force=1 이면 그래도 저장)."""
        ok, err = _live(request)
        if err:
            return err
        link, _u, j = ok
        q = request.query_params
        cur = f"v{j.current.v}"
        if q.get("base") and q.get("base") != cur and q.get("force") != "1":
            return JSONResponse({"error": "conflict", "current": cur}, status_code=409)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > cfg.max_upload_mb * 1048576:
                return JSONResponse({"error": f"{cfg.max_upload_mb}MB 를 넘는다"}, status_code=413)
        if len(body) < 64 or not (body[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or body[:2] == b"PK"):
            return JSONResponse({"error": "HWP/HWPX 로 보이지 않는 내용이라 저장하지 않았다"}, status_code=400)
        ext = j.current.file.rsplit(".", 1)[-1]
        tmp = j.dir / f".incoming.{ext}"
        tmp.write_bytes(bytes(body))
        auto = q.get("auto") == "1"
        if auto and j.current.by == "editor-auto" and j.current.v > 0:
            v = sandbox.amend(j, tmp, by="editor-auto", note=f"{link.who} 자동 저장")
        else:
            v = sandbox.commit(j, tmp, by="editor-auto" if auto else "editor",
                               note=f"{link.who} {'자동 저장' if auto else '저장'}")
        return JSONResponse({"version": f"v{v.v}", "bytes": len(body)})

    @mcp.custom_route("/live/{token}/presence", methods=["POST"])
    async def live_presence(request: Request):
        """실시간 화면이 살아 있고 저장 안 된 사람 수정이 있는지 (Claude 편집이 기다릴지 판단)."""
        ok, err = _live(request)
        if err:
            return err
        _l, _u, j = ok
        q = request.query_params
        (j.dir / ".human").write_text(json.dumps({"t": time.time(), "dirty": q.get("dirty") == "1", "v": q.get("v")}),
                                      encoding="utf-8")
        return JSONResponse({"ok": True})

    @mcp.custom_route("/live/{token}/restore", methods=["POST"])
    async def live_restore(request: Request):
        """옛 판을 그대로 새 판으로 올린다 (되돌리기 — 이력은 지우지 않는다)."""
        ok, err = _live(request)
        if err:
            return err
        link, _u, j = ok
        try:
            src = _ver_path(j, request.query_params.get("v"))
        except LinkError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        tmp = j.dir / f".incoming{src.suffix}"
        shutil.copyfile(src, tmp)
        v = sandbox.commit(j, tmp, by="restore", note=f"{src.stem} 으로 되돌림")
        return JSONResponse({"version": f"v{v.v}"})

    def _lines(user: str, j: Job, v: str) -> list[tuple[int, str]]:
        """판의 본문을 (쪽, 줄) 로. 판 파일은 바뀌지 않으므로 파일 크기·시각으로 캐시한다."""
        f = _ver_path(j, v)
        st = f.stat()
        cache = j.dir / f".text-{f.stem}-{st.st_size}-{int(st.st_mtime)}.json"
        if cache.is_file():
            return [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
        viewer = Principal(label="live", email="live@hwp-cowork", roots=frozenset(), username=user)
        out = runner.run(viewer, "hwp_export_text", {"path": f.stem}, j)
        res = out.get("result") or {}
        if out.get("result_truncated") and out.get("full_result"):
            res = json.loads((j.out / out["full_result"].removeprefix("out/")).read_text(encoding="utf-8"))
            (j.out / out["full_result"].removeprefix("out/")).unlink(missing_ok=True)
        lines: list[tuple[int, str]] = []
        for pg in res.get("pages", []):
            run = ""          # 좁은 표 칸은 한 줄에 한두 글자씩 풀려 나온다 — 짧은 줄이 이어지면 한 줄로 붙인다
            for ln in (pg.get("text") or "").splitlines():
                t = ln.strip()
                if not t:
                    continue
                if len(t) <= 3:
                    run += t
                    continue
                if run:
                    lines.append((pg.get("page", 0) + 1, run)); run = ""
                lines.append((pg.get("page", 0) + 1, t))
            if run:
                lines.append((pg.get("page", 0) + 1, run))
        for old in j.dir.glob(f".text-{f.stem}-*.json"):
            old.unlink(missing_ok=True)
        cache.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
        return lines

    def _text_diff(user: str, j: Job, a: str, b: str, limit: int = 300) -> dict:
        import difflib
        la, lb = _lines(user, j, a), _lines(user, j, b)
        sm = difflib.SequenceMatcher(a=[x[1] for x in la], b=[x[1] for x in lb], autojunk=False)
        changes = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            olds, news = la[i1:i2], lb[j1:j2]
            if len(olds) == len(news):          # 줄 수가 같으면 줄끼리 짝지어 글자 차이까지 보여준다
                pairs = list(zip(olds, news))
            else:                               # 아니면 한 덩어리로
                pairs = [((olds[0][0], "\n".join(x[1] for x in olds)) if olds else None,
                          (news[0][0], "\n".join(x[1] for x in news)) if news else None)]
            for o, n in pairs:
                changes.append({"type": "change" if o and n else ("del" if o else "add"),
                                "page": (n or o)[0], "old": o[1] if o else None, "new": n[1] if n else None})
        return {"a": a, "b": b, "count": len(changes), "changes": changes[:limit], "truncated": len(changes) > limit,
                "pagesA": la[-1][0] if la else 0, "pagesB": lb[-1][0] if lb else 0}

    @mcp.custom_route("/live/{token}/diff", methods=["GET"])
    async def live_diff(request: Request):
        """두 판의 본문 차이 (문단 단위, 쪽 번호). 사람이 읽는 비교 — 구조 비교는 hwp_diff 도구."""
        ok, err = _live(request)
        if err:
            return err
        _l, user, j = ok
        a, b = request.query_params.get("a", "v0"), request.query_params.get("b", f"v{j.current.v}")
        try:
            out = await asyncio.to_thread(_text_diff, user, j, a, b)
        except (RunError, SandboxError, LinkError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(out)

    def _owner(user: str) -> Principal:
        if registry is None:
            return LOCAL
        p = registry.principals.get(user) or next((x for x in registry.static.values() if x.username == user), None)
        if p is None:
            raise ToolError("링크 주인을 찾을 수 없다.")
        return p

    def _default_folder(user: str, j: Job) -> str:
        if j.source and "/" in j.source:
            return j.source.rsplit("/", 1)[0]
        roots = _owner(user).visible_roots(all_roots)
        return roots[0] if roots else ""

    @mcp.custom_route("/live/{token}/folders", methods=["GET"])
    async def live_folders(request: Request):
        """폴더에 저장 창의 폴더 고르기 — path 가 없으면 볼 수 있는 루트, 있으면 그 아래 폴더."""
        ok, err = _live(request)
        if err:
            return err
        _l, user, j = ok
        try:
            who = _owner(user)
        except ToolError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        path = (request.query_params.get("path") or "").strip().strip("/")
        if not path:
            return JSONResponse({"path": "", "folders": who.visible_roots(all_roots), "default": _default_folder(user, j)})
        try:
            root, rel, real = resolve(cfg.roots, path)
        except PathError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not who.can_see(root) or not real.is_dir():
            return JSONResponse({"error": "볼 수 없는 폴더"}, status_code=403)

        def ls() -> list[str]:
            out = []
            for d in real.iterdir():
                if d.name.startswith((".", "@", "#")) or d.is_symlink():
                    continue
                try:
                    if d.is_dir():
                        out.append(d.name)
                except OSError:
                    continue
            return sorted(out, key=str.lower)[:1000]
        return JSONResponse({"path": f"{root}/{rel}".rstrip("/"), "folders": await asyncio.to_thread(ls)})

    @mcp.custom_route("/live/{token}/apply", methods=["POST"])
    async def live_apply(request: Request):
        """폴더에 저장 — 고른 폴더에 고른 이름으로 사본을 넣는다 (같은 이름이 있으면 서버가 _vN 을 붙인다).
        원본 덮어쓰기는 하지 않는다. 쓰기 권한자 링크만."""
        ok, err = _live(request)
        if err:
            return err
        link, user, j = ok
        if not link.rw:
            return JSONResponse({"error": "폴더 쓰기 권한이 없다"}, status_code=403)
        q = request.query_params
        folder = (q.get("folder") or "").strip().strip("/") or _default_folder(user, j)
        name = (q.get("name") or "").strip() or None
        try:
            root, _rel, real = resolve(cfg.roots, folder)
            if not _owner(user).can_see(root) or not real.is_dir():
                raise ToolError("그 폴더에는 저장할 수 없다.")
            p = _ver_path(j, q.get("v"))
            body = await asyncio.to_thread(_nas_put, user, j, p, "copy", folder, name)
        except (LinkError, ToolError, PathError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"mode": "copy", "version": p.stem, **body})

    UP_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
               "Access-Control-Allow-Headers": "content-type, x-filename", "Access-Control-Max-Age": "600"}
    DOC_MAGIC = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"<?xml", b"<HWPML", b"\xef\xbb\xbf<")

    @mcp.custom_route("/hu/{token}", methods=["GET", "POST", "PUT", "OPTIONS"])
    async def hwp_upload(request: Request):
        """첨부 파일 올리기 — Claude 코드 실행의 curl(multipart 또는 본문 그대로 + ?name=), 채팅 안 업로드 칸(fetch),
        브라우저(GET 은 작은 올리기 화면). 코드가 곧 인증이고(30분), 올린 사람 = 링크를 만든 사람의 새 작업이 된다."""
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=UP_CORS)
        if throttle.blocked(request):
            return JSONResponse({"error": "잠시 뒤 다시 시도할 것"}, status_code=429, headers=UP_CORS)
        try:
            link = signer.verify("hu", request.path_params["token"])
        except LinkError as e:
            throttle.fail(request)
            return JSONResponse({"error": str(e)}, status_code=403, headers=UP_CORS)
        if request.method == "GET":
            return HTMLResponse(UPLOAD_PAGE.replace("__WHO__", _esc(link.who)), headers=UP_CORS)
        limit = cfg.max_upload_mb * 1048576
        total = int(request.headers.get("content-length") or 0)
        if total > limit + 65536:
            return JSONResponse({"error": f"{cfg.max_upload_mb}MB 를 넘는다"}, status_code=413, headers=UP_CORS)
        # 받는 만큼 진행률을 남긴다 — 채팅 안 화면이 curl 업로드도 막대로 보여 준다 (hwp_app_uploaded)
        code = request.path_params["token"]
        buf = bytearray()
        try:
            async for chunk in request.stream():
                buf += chunk
                UP_PROGRESS[code] = (len(buf), total, time.time())
                if len(buf) > limit + 65536:
                    return JSONResponse({"error": f"{cfg.max_upload_mb}MB 를 넘는다"}, status_code=413, headers=UP_CORS)
        finally:
            UP_PROGRESS.pop(code, None)
        raw = bytes(buf)

        async def replay():
            return {"type": "http.request", "body": raw, "more_body": False}
        request = Request(request.scope, receive=replay)
        if request.headers.get("content-type", "").startswith("multipart/"):
            form = await request.form(max_files=1, max_fields=2)
            up = form.get("file")
            if up is None or not getattr(up, "filename", ""):
                return JSONResponse({"error": "file 필드가 없다 (curl -F \"file=@경로\")"}, status_code=400, headers=UP_CORS)
            name, data = (form.get("name") or up.filename), await up.read()
        else:
            from urllib.parse import unquote
            name = request.query_params.get("name") or unquote(request.headers.get("x-filename", ""))
            data = await request.body()
        name = _clean_name(name)
        if not name or name.startswith(".") or Path(name).suffix.lower() not in DOC_EXT:
            return JSONResponse({"error": f"한글 문서(.hwp .hwpx .hml) 이름이 아니다: {name!r}"}, status_code=400, headers=UP_CORS)
        if len(data) > limit:
            return JSONResponse({"error": f"{cfg.max_upload_mb}MB 를 넘는다"}, status_code=413, headers=UP_CORS)
        if not data.startswith(DOC_MAGIC):
            return JSONResponse({"error": "한글 파일로 보이지 않는다"}, status_code=400, headers=UP_CORS)
        user = link.path
        try:
            tmp = Path(tempfile.mkdtemp(prefix=".up-", dir=sandbox.user_dir(user)))
            try:
                src = tmp / ("up" + Path(name).suffix.lower())
                src.write_bytes(data)
                j = sandbox.create(user, src, "", name)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        except SandboxError as e:
            return JSONResponse({"error": str(e)}, status_code=400, headers=UP_CORS)
        j.versions[0].by, j.versions[0].note = "upload", "채팅에서 올림"
        j.live["upload_code"] = request.path_params["token"]
        sandbox.save_meta(j)
        threading.Thread(target=_warm, args=(j.path(),), daemon=True).start()   # 화면이 열 1쪽을 미리 그려 둔다
        log.info("올림 %s: %s → %s (%d bytes)", link.who, name, j.id, len(data))
        return JSONResponse({"job": j.id, "name": name, "bytes": len(data),
                             "next": f'hwp 도구의 doc 에 "{j.id}" 를 쓴다',
                             # curl 응답은 캐시되지 않는 그 자리 지시라 도구 설명보다 잘 듣는다
                             "for_claude": f'올라갔다. 채팅 안 화면이 이미 이 문서를 쪽 넘기며 보여 주고 있으니 "보여줘"에는 hwp_preview 를 '
                                           f'부르지 말고 hwp_text(doc="{j.id}") 로 내용만 읽어 짧게 요약한다. 코드 실행으로 이 파일의 '
                                           f'압축을 풀거나 XML 을 직접 읽지 말 것 (hwp 도구가 실패했을 때만 최후의 수단).'}, headers=UP_CORS)

    @mcp.custom_route("/dl/{token}", methods=["GET"])
    async def download(request: Request):
        """다운로드 링크는 로그인 없이 코드만으로 연다 (받기 전용·짧은 수명). 대신 틀린 코드 대입은 막는다."""
        if throttle.blocked(request):
            return JSONResponse({"error": "잠시 뒤 다시 시도할 것"}, status_code=429)
        try:
            link = signer.verify("dl", request.path_params["token"])
            user, jid, fname = link.path.split("/", 2)
            j = sandbox.get(user, jid)
            p = (j.out / fname[4:]) if fname.startswith("out/") else (j.dir / fname)
            if not p.is_file() or p.resolve().parent not in (j.dir.resolve(), j.out.resolve()):
                raise LinkError("파일이 없다 (작업이 만료됐을 수 있다)")
        except (LinkError, SandboxError, ValueError) as e:
            throttle.fail(request)
            return JSONResponse({"error": str(e)}, status_code=403)
        name = p.name if p.parent == j.out else f"{j.name.rsplit('.', 1)[0]}_{p.stem}{p.suffix}"
        return FileResponse(str(p), filename=name)

    return mcp, sandbox, updates


def _clean_name(name: str | None) -> str:
    """claude.ai 첨부 이름 정리: 경로·`<uuid>-<시각>_` 머리·한글이 바뀐 밑줄 덩어리를 걷어낸다."""
    n = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    n = re.sub(r"^[0-9a-f]{8}-(\d{10,})?[_-]*", "", n)
    n = re.sub(r"_{3,}", "_", n).strip("_ ")
    return n if not n.startswith(".") else "문서" + n


UPLOAD_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>한글 파일 올리기</title><style>
:root{--fg:#1f2328;--mut:#6b7280;--line:#d0d7de;--acc:#2E62B8;--bg:#fff}
@media (prefers-color-scheme:dark){:root{--fg:#e8e8e8;--mut:#9ca3af;--line:#3c3f44;--acc:#8fb0f0;--bg:#18191b}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}
main{max-width:520px;margin:12vh auto;padding:0 16px}label{display:block;border:2px dashed var(--line);border-radius:10px;
padding:44px 16px;text-align:center;color:var(--mut);cursor:pointer}label.on{border-color:var(--acc);color:var(--acc)}
input{display:none}#m{margin-top:14px;color:var(--mut)}</style></head><body><main><h3>한글 파일 올리기 · __WHO__</h3>
<label id="d"><input id="f" type="file" accept=".hwp,.hwpx,.hml">여기에 끌어다 놓거나 눌러서 고르세요</label><p id="m"></p>
<script>const d=document.getElementById('d'),m=document.getElementById('m');
['dragenter','dragover'].forEach(t=>d.addEventListener(t,e=>{e.preventDefault();d.classList.add('on')}));
['dragleave','drop'].forEach(t=>d.addEventListener(t,e=>{e.preventDefault();d.classList.remove('on')}));
d.addEventListener('drop',e=>e.dataTransfer.files[0]&&go(e.dataTransfer.files[0]));
document.getElementById('f').onchange=e=>e.target.files[0]&&go(e.target.files[0]);
async function go(f){m.textContent='올리는 중… '+f.name;const fd=new FormData();fd.append('file',f,f.name);
const r=await fetch(location.pathname,{method:'POST',body:fd});const o=await r.json().catch(()=>({}));
m.textContent=r.ok?('올렸습니다: '+o.name+' (작업 '+o.job+') — 채팅으로 돌아가 "올렸어"라고 하세요'):('실패: '+(o.error||r.status))}
</script></main></body></html>"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    registry = oidc = None
    if cfg.transport == "http":
        try:
            registry, oidc = build_registry(list(cfg.roots), mode=cfg.auth)
        except (AuthConfigError, ValueError, OSError) as e:
            print(f"인증 설정 오류: {e}", file=sys.stderr)
            return 2
        if registry is None:
            log.warning("인증 없음 (HWPCOWORK_AUTH=none) — 이 주소에 닿는 누구나 모든 문서를 읽고 고칠 수 있다. "
                        "믿을 수 있는 망이나 인증 프록시 뒤에서만 쓸 것.")
        else:
            log.info("인증 %s — 주체: %s", cfg.auth, ", ".join(registry.labels))
    serve(cfg, registry, oidc)
    return 0


def serve(cfg: Config, registry: Registry | None, oidc) -> None:
    """main 과 종단 테스트(가짜 주체 Registry)가 같이 쓴다."""
    mcp, sandbox, updates = build(cfg, registry)
    sandbox.start_sweeper()
    updates.start_background()
    log.info("hwp-cowork 시작: roots=%s work=%s", {k: str(v) for k, v in cfg.roots.items()}, cfg.work_dir)
    if cfg.transport == "stdio":
        mcp.run("stdio")
        return

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import PlainTextResponse

    from .auth import AuthMiddleware

    @mcp.custom_route("/internal/health", methods=["GET"])
    async def internal_health(request: Request):
        """모니터링용 — 작업 공간(/tmp·/work)·메모리·최근 실패(메모리 초과·공간 부족·시간 초과).
        HWPCOWORK_HEALTH_KEY 가 있으면 X-Health-Key 헤더가 맞아야 한다. Cloudflare 터널을 거친 요청은 거부한다."""
        from starlette.responses import JSONResponse
        from .runner import health
        import hmac as _hmac
        if request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip"):
            return JSONResponse({"error": "internal only"}, status_code=403)
        key = os.environ.get("HWPCOWORK_HEALTH_KEY")
        if key and not _hmac.compare_digest(request.headers.get("x-health-key", ""), key):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(health())

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):
        return PlainTextResponse("ok")

    security = TransportSecuritySettings(enable_dns_rebinding_protection=bool(cfg.allowed_hosts),
                                         allowed_hosts=cfg.allowed_hosts, allowed_origins=cfg.allowed_origins)
    app = mcp.streamable_http_app(transport_security=security, host=cfg.host)
    asgi = app if registry is None else AuthMiddleware(app, registry, oidc, realm="hwp-cowork", open_paths=OPEN_PATHS)
    # X-Forwarded-* 는 믿을 수 있는 프록시에서 온 것만 받는다 (기본: 같은 기계). 아니면 누구나 출처 IP 를 꾸밀 수 있다
    uvicorn.run(asgi, host=cfg.host, port=cfg.port, log_level="info", proxy_headers=True,
                forwarded_allow_ips=os.environ.get("HWPCOWORK_FORWARDED_ALLOW_IPS") or "127.0.0.1")


if __name__ == "__main__":
    raise SystemExit(main())
