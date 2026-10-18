"""rhwp 의 모든 기능을 샌드박스 안에서 돌린다.

기능 목록은 rhwp 자신이 낸다 (`rhwp capabilities --mcp` — 도구마다 입력 스키마와 CLI 배선). 그래서 rhwp 를
올리면 새 기능이 코드 수정 없이 들어온다. 이 모듈의 일은 **경로 인자를 전부 서버가 바꿔 넣는 것**이다:

  문서 입력 (path a b pathB form)   "v2"·"current"(작업의 판) · "out/파일"(작업 부산물) · 문서 경로(읽기만)
  기타 입력 (csv image keyring …)   "out/파일" 또는 문서 경로(읽기만)
  출력 (output out outDir …)        작업 out/ 아래로 강제 (이름만 쓴다)
  paths (배치)                       문서 경로·폴더(안의 .hwp/.hwpx 로 펼침) 또는 작업 판

경로처럼 보이는데 어느 칸에도 안 드는 인자가 있는 기능은 **막는다** (fail-closed) — 새 rhwp 가 모르는 경로 인자를
들고 와도 샌드박스 밖을 건드리지 못한다. 편집 기능이 만든 새 문서는 다음 판(vN)으로 등록된다.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import builder
from .fsafe import PathError, resolve
from .sandbox import DOC_EXT, Job, Sandbox, SandboxError

log = logging.getLogger("hwp_cowork.run")

DOC_IN = {"path", "a", "b", "pathB", "form"}
OUT = {"output", "out", "outDir", "openingOut"}

# 글꼴은 이미지가 책임진다(fonts-noto-cjk + docker/fonts-ko.conf) — rhwp 기본 예비 글꼴(Noto CJK)이 있으니 비워 둔다.
# 필요하면 HWPCOWORK_PDF_FONT_ARGS="--fallback-sans NanumGothic ..." 로 export-pdf 에 덧붙인다
PDF_FONT_ARGS = os.environ.get("HWPCOWORK_PDF_FONT_ARGS", "").split()

# 일부 쪽 PDF (hwp_export_pdf 의 pages) — rhwp export-pdf 는 코어 하나만 쓰고 -p 로 한 쪽씩만 받는다. hwp_split_document 는
# 문단 단위로 지워 재조판하므로 쪽이 달라진다 (124구역 문서에서 1~20쪽 → 141쪽, 2026-10-14). 그래서 쪽마다 따로 그려
# qpdf 로 잇는다. 한 쪽 그리기는 문서 전체를 다시 읽으므로(376MB 문서: 쪽당 2.9초·1.5GB) 동시 실행 수로 메모리를 묶는다
PDF_PAGE_WORKERS = int(os.environ.get("HWPCOWORK_PDF_PAGE_WORKERS", "4"))
PDF_PAGES_MAX = 300
PDF_PAGES_PROP = {"type": "string",
                  "description": "일부 쪽만 PDF 로 — '1-20' · '3,5,7-9' (1부터). 쪽마다 원본 모양 그대로 그려 이어 붙인다. "
                                 "비우면 전체"}


def parse_pages(spec: str, total: int) -> list[int]:
    """'1-20' · '3,5,7-9' · '480-' → 1부터 세는 쪽 번호 (순서·중복 정리)."""
    out: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d*))?", part)
        if not m:
            raise RunError(f"pages 형식: '1-20', '3,5,7-9' — 받은 것 {spec!r}")
        a = int(m.group(1))
        b = a if m.group(2) is None else (int(m.group(2)) if m.group(2) else total)
        if a < 1 or b > total or a > b:
            raise RunError(f"쪽 범위 {part} 가 문서 밖이다 (전체 {total}쪽).")
        out += [p for p in range(a, b + 1) if p not in out]
    if not out:
        raise RunError("pages 가 비었다.")
    if len(out) > PDF_PAGES_MAX:
        raise RunError(f"한 번에 {PDF_PAGES_MAX}쪽까지 — 전체가 필요하면 pages 없이 부를 것.")
    return out


# 상태 페이지용 — 최근 작업 실패(메모리 초과·공간 부족). 2026-10-14 큰 문서 사고가 상태 페이지에 안 잡혀서 넣었다
FAILS: "collections.deque[tuple[float, str, str]]" = collections.deque(maxlen=200)
LAST_OK = [0.0]      # 마지막으로 rhwp 가 정상 끝난 시각 — 이 뒤의 실패만 '안 풀린 실패'


def note_failure(rc: int | None, stderr: str, what: str) -> None:
    reason = ("oom" if rc in (-9, 137) else "nospace" if "No space left" in (stderr or "")
              else "timeout" if rc == "timeout" else None)
    if reason:
        FAILS.append((time.time(), reason, what))
    elif rc == 0:
        LAST_OK[0] = time.time()


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def health() -> dict:
    """작업 공간·메모리·최근 실패. 판정은 모니터링 쪽이 한다. 경로·문서 이름은 내보내지 않는다."""
    def du(p):
        try:
            u = shutil.disk_usage(p)
            return {"used_pct": round(u.used * 100 / u.total, 1), "free_gb": round(u.free / 1e9, 1)}
        except OSError:
            return None
    mem_cur, mem_max = _read("/sys/fs/cgroup/memory.current"), _read("/sys/fs/cgroup/memory.max")
    events = dict(line.split() for line in _read("/sys/fs/cgroup/memory.events").splitlines() if " " in line)
    now = time.time()
    recent = [f for f in FAILS if now - f[0] < 3600]
    return {
        "tmp": du("/tmp"), "work": du(os.environ.get("HWPCOWORK_WORK_DIR", "/work")),
        "mem_used_mb": int(mem_cur) // 1048576 if mem_cur.isdigit() else None,
        "mem_limit_mb": int(mem_max) // 1048576 if mem_max.isdigit() else None,
        "oom_kills": int(events.get("oom_kill", 0)),
        "failed_1h": {r: sum(1 for f in recent if f[1] == r) for r in ("oom", "nospace", "timeout")},
        "last_failure_s": int(now - FAILS[-1][0]) if FAILS else None,
        # 안 풀린 실패 — 마지막 정상 작업 뒤의 실패 (24시간 안). 고친 뒤 작업 하나가 끝나면 0 (상태 페이지 경고는 이것으로)
        "failed_unresolved": {r: sum(1 for f in FAILS if f[1] == r and f[0] > max(LAST_OK[0], now - 86400))
                              for r in ("oom", "nospace", "timeout")},
        "last_ok_s": int(now - LAST_OK[0]) if LAST_OK[0] else None,
    }


def _ensure_dirs() -> None:
    """TMPDIR·글꼴 캐시 폴더가 없으면 만든다. TMPDIR(/work/.tmp)이 없으면 파이썬이 조용히 /tmp(256MB tmpfs)를 써서
    큰 문서 SVG 가 그곳을 채웠다 (2026-10-14 No space left on device)."""
    for var in ("TMPDIR", "XDG_CACHE_HOME"):
        d = os.environ.get(var)
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = None      # gettempdir() 를 다시 계산하게
FILE_IN = {"csv", "image", "capsule", "keyring", "dir", "log", "checkpoint", "policy", "gateEnvelope", "workorder",
           "claim", "sig", "signKey", "domain", "head", "bundle", "trustDomain", "opening", "redacted", "among",
           "contaminated", "anchorLog", "ledger"}
CREATE_TOOLS = {"hwp_scaffold", "hwp_build_from_ingest"}     # path 가 문서가 아니라 명세 JSON 이다 → hwp_new
_PATHY = re.compile(r"경로|폴더|파일|path|dir", re.I)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_RETURN = 60_000
MAX_BATCH = 5000


class RunError(ValueError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    cli: dict
    read_only: bool
    destructive: bool
    blocked: str | None = None     # 막힌 이유

    @property
    def props(self) -> dict:
        return self.schema.get("properties", {})

    @property
    def required(self) -> list[str]:
        return self.schema.get("required", [])


class Manifest:
    def __init__(self, rhwp: str) -> None:
        r = subprocess.run([rhwp, "capabilities", "--mcp"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"rhwp capabilities 실패: {r.stderr[:300]}")
        raw = json.loads(r.stdout)
        self.stdin_tools = set(raw.get("invocation", {}).get("stdinTools", []))
        self.tools: dict[str, Tool] = {}
        for t in raw["tools"]:
            a = t.get("annotations", {})
            tool = Tool(t["name"], t.get("description", ""), t.get("inputSchema", {}), t["cli"],
                        bool(a.get("readOnlyHint")), bool(a.get("destructiveHint")))
            tool.blocked = self._check(tool)
            self.tools[tool.name] = tool
        pdf = self.tools.get("hwp_export_pdf")
        if pdf and "pages" not in pdf.props:
            pdf.schema = {**pdf.schema, "properties": {**pdf.props, "pages": PDF_PAGES_PROP}}
            pdf.description += (" (이 서버) pages='1-20' 처럼 주면 그 쪽들만 원본 모양 그대로 PDF 로 — 문서 일부를 PDF 로 낼 때는 "
                                "hwp_split_document 대신 이것 (split 은 재조판돼 쪽이 달라진다).")
        split = self.tools.get("hwp_split_document")
        if split:
            split.description += " (이 서버) 일부 쪽을 PDF 로 원하면 이것 말고 hwp_export_pdf 의 pages 를 쓴다."
        blocked = {n: t.blocked for n, t in self.tools.items() if t.blocked}
        if blocked:
            log.warning("막은 기능 %d개: %s", len(blocked), blocked)

    def _check(self, t: Tool) -> str | None:
        for k, v in t.props.items():
            if k in DOC_IN | OUT | FILE_IN | {"paths", "plan", "password"}:
                continue
            if v.get("enum"):
                continue
            if v.get("type") == "string" and _PATHY.search(k + " " + (v.get("description") or "")) \
                    and not re.search(r"문자열|이름|형식|JSON|기대", v.get("description") or ""):
                return f"분류되지 않은 경로 인자 {k!r} — 샌드박스를 보장할 수 없어 막았다"
        if "plan" in t.props and t.props["plan"].get("type") != "object":
            return "plan 형식을 알 수 없다"
        return None

    def get(self, name: str) -> Tool:
        t = self.tools.get(name)
        if t is None:
            near = [n for n in self.tools if name.replace("hwp_", "") in n][:5]
            raise RunError(f"그런 기능이 없다: {name!r}. hwp_catalog 로 찾을 것" + (f" (비슷한 것: {near})" if near else ""))
        if t.blocked:
            raise RunError(f"{name} 은(는) 이 서버에서 막혀 있다: {t.blocked}")
        return t

    def search(self, query: str | None, limit: int = 40) -> list[dict]:
        items = [t for t in self.tools.values() if not t.blocked]
        if query:
            words = [w.lower() for w in query.split() if w]
            def score(t: Tool) -> int:
                hay = (t.name + " " + t.description).lower()
                return sum(hay.count(w) for w in words) + sum(5 for w in words if w in t.name)
            items = sorted((t for t in items if score(t) > 0), key=score, reverse=True)[:limit]
            return [{"name": t.name, "description": t.description, "readOnly": t.read_only,
                     "required": t.required, "inputs": {k: _brief(v) for k, v in t.props.items() if k != "password"}}
                    for t in items]
        return [{"name": t.name, "summary": t.description.split(".")[0].split(" — ")[0][:80],
                 **({"edits": True} if not t.read_only else {})} for t in sorted(items, key=lambda t: t.name)]


def _brief(v: dict) -> str:
    s = v.get("type", "")
    if v.get("enum"):
        s += " " + "|".join(map(str, v["enum"]))
    d = (v.get("description") or "").strip()
    return f"{s} — {d[:120]}" if d else s


class Runner:
    def __init__(self, rhwp: str, manifest: Manifest, sandbox: Sandbox, roots: dict[str, Path], timeout: int = 300) -> None:
        self.rhwp, self.m, self.sb, self.roots, self.timeout = rhwp, manifest, sandbox, roots, timeout
        self._png_cache: dict[tuple, bytes] = {}
        self._svg_cache: dict[tuple, Path] = {}        # (문서, 수정시각, 쪽) → SVG
        self._svg_dirs: dict[tuple, Path] = {}         # (문서, 수정시각) → 그 문서 쪽 SVG 폴더
        self._pages_cache: dict[tuple, int] = {}
        self._render_lock = threading.Lock()
        _ensure_dirs()

    # ---- 경로 바꿔 넣기 ----------------------------------------------------

    def _nas(self, who, ref: str) -> Path | None:
        """문서 가상 경로면 실제 경로 (읽기 전용 마운트), 아니면 None."""
        head = ref.replace("\\", "/").strip("/").split("/", 1)[0]
        if head not in self.roots:
            return None
        try:
            root, _rel, real = resolve(self.roots, ref)
        except PathError as e:
            raise RunError(str(e)) from None
        if not who.can_see(root):
            raise RunError(f"{who.label} 은(는) 루트 {root!r} 를 볼 수 없다.")
        if not real.exists():
            raise RunError(f"폴더에 없다: {ref}")
        return real

    def _job_file(self, job: Job | None, ref: str, key: str) -> Path:
        r = (ref or "current").strip()
        if job is None:
            raise RunError(f"{key}={ref!r}: 작업 파일을 쓰려면 doc(NAS 경로 또는 작업 ID)이 필요하다.")
        if r in ("current", "latest"):
            return job.path()
        if re.fullmatch(r"v\d+", r):
            return job.path(int(r[1:]))
        name = r.removeprefix("out/")
        if "/" in name or name.startswith(".") or not name:
            raise RunError(f"{key}={ref!r}: 작업 안 파일은 'v2' 또는 'out/이름' 꼴로 준다.")
        p = job.out / name
        if not p.exists():
            raise RunError(f"{key}={ref!r}: 작업 {job.id} 에 없다. 있는 것: {job.summary()['files']}")
        return p

    def _input(self, who, job: Job | None, ref: Any, key: str) -> str:
        if key == "contaminated" and isinstance(ref, str) and _SHA256.fullmatch(ref):
            return ref
        if not isinstance(ref, str):
            raise RunError(f"{key} 는 문자열 경로여야 한다.")
        nas = self._nas(who, ref)
        return str(nas) if nas is not None else str(self._job_file(job, ref, key))

    def _output(self, job: Job | None, ref: Any, key: str) -> str:
        if job is None:
            raise RunError(f"{key}: 파일을 만드는 기능은 doc(NAS 경로 또는 작업 ID)을 줘야 한다 — 결과는 그 사본의 out/ 에 생긴다.")
        name = Path(str(ref or f"{key}")).name
        if not name or name.startswith(".") or name in ("..",):
            raise RunError(f"{key}: 쓸 수 없는 이름 {ref!r}")
        return str(job.out / name)

    def _expand(self, who, job: Job | None, refs: list) -> list[str]:
        out: list[str] = []
        for ref in refs:
            nas = self._nas(who, ref) if isinstance(ref, str) else None
            if nas is not None and nas.is_dir():
                for dp, dns, fns in os.walk(nas):
                    dns[:] = [d for d in dns if not d.startswith((".", "@", "#"))]
                    out += [os.path.join(dp, f) for f in fns if Path(f).suffix.lower() in {".hwp", ".hwpx"}]
                    if len(out) > MAX_BATCH:
                        raise RunError(f"문서가 {MAX_BATCH}개를 넘는다 — 폴더를 좁힐 것.")
            else:
                out.append(self._input(who, job, ref, "paths"))
        return out

    # ---- 실행 --------------------------------------------------------------

    def create(self, name: str, spec: dict, workdir: Path) -> tuple[Path, Any]:
        """명세(JSON)에서 새 문서를 만든다 — 원본이 없는 생성 기능. 명세는 서버가 workdir 에 쓰고, 결과도 그 안에 둔다."""
        t = self.m.get(name)
        if name not in CREATE_TOOLS:
            raise RunError(f"{name} 은(는) 문서를 만드는 기능이 아니다. 만드는 기능: {sorted(CREATE_TOOLS)}")
        sp, out = workdir / "spec.json", workdir / "new.hwpx"
        sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        cmd = [self.rhwp] + [_fill(a, {"path": str(sp), "output": str(out)}) for a in t.cli["args"]]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, cwd=workdir)
        except subprocess.TimeoutExpired:
            raise RunError(f"{self.timeout}초 안에 끝나지 않았다.") from None
        if r.returncode != 0 or not out.is_file():
            msg = (r.stderr.strip() or r.stdout.strip()).replace(str(workdir) + "/", "")
            raise RunError(f"문서를 만들지 못했다: {msg[:1500]}")
        try:
            return out, json.loads(r.stdout)
        except ValueError:
            return out, None

    def compose(self, spec: dict, workdir: Path, fmt: str = "hwpx") -> tuple[Path, dict]:
        """서식 명세(builder)로 새 문서를 만든다. 포장은 scaffold 가 만든 빈 HWPX 를 빌리고, 결과는 rhwp 로 다시 읽어
        검증한다 — rhwp 가 못 읽는 문서는 내보내지 않는다. fmt="hwp" 면 HWP5 로 바꾼다(변환 검증 포함)."""
        skel, _ = self.create("hwp_scaffold", {"version": "1", "blocks": [{"type": "paragraph", "text": "x"}]}, workdir)
        out = workdir / "composed.hwpx"
        try:
            data, b = builder.build(spec, skel.read_bytes())
            out.write_bytes(data)
            toc = {}
            if b.toc_auto:                       # 목차 쪽 자동: 한 번 그려 실제 쪽을 찾고 채워서 다시
                toc = builder.toc_pages_from_text(self._page_texts(out, workdir), b.toc_auto, b.restart)
                data, b = builder.build(spec, skel.read_bytes(), toc)
                out.write_bytes(data)
        except builder.SpecError as e:
            raise RunError(f"spec 오류: {e}") from None
        r = subprocess.run([self.rhwp, "info", str(out), "--json"], capture_output=True, text=True,
                           timeout=self.timeout, cwd=workdir)
        if r.returncode != 0:
            raise RunError(f"만든 문서를 rhwp 가 읽지 못했다 (명세를 줄여 다시): {r.stderr.strip()[:800]}")
        try:
            info = json.loads(r.stdout)
        except ValueError:
            info = {}
        summary = {"pages": info.get("pageCount"), "bytes": len(data), "format": "hwpx",
                   **({"toc_pages": toc} if toc else {})}
        if fmt == "hwp":
            hwp = workdir / "composed.hwp"
            r = subprocess.run([self.rhwp, "convert", str(out), str(hwp), "--verify", "--json"], capture_output=True,
                               text=True, timeout=self.timeout, cwd=workdir)
            if r.returncode != 0 or not hwp.is_file():
                raise RunError(f"HWP 변환 실패 (hwpx 로 만들 것): {(r.stderr or r.stdout).strip()[:800]}")
            summary.update(format="hwp", bytes=hwp.stat().st_size)
            out = hwp
        return out, summary

    def _page_texts(self, doc: Path, workdir: Path) -> list[str]:
        r = subprocess.run([self.rhwp, "export-text", str(doc), "--json"], capture_output=True, text=True,
                           timeout=self.timeout, cwd=workdir)
        try:
            return [p.get("text") or "" for p in json.loads(r.stdout).get("pages", [])]
        except (ValueError, AttributeError):
            return []

    # ---- 쪽 그림 — hwp_preview 와 채팅 안 화면이 같이 쓴다 ----------------------------------------
    # export-svg 는 문서 전체를 한 번에 그리므로 (파일, 수정시각) 마다 한 번만 돌리고 SVG 를 남겨 둔다.
    # 쪽 PNG 도 기억한다. 같은 판을 Claude 의 미리보기와 화면이 번갈아 봐도 rhwp 는 한 번만 돈다.

    def page_count(self, doc: Path) -> int:
        key = (str(doc), doc.stat().st_mtime)
        if key not in self._pages_cache:
            r = subprocess.run([self.rhwp, "info", str(doc), "--json"], capture_output=True, text=True, timeout=self.timeout)
            try:
                self._pages_cache[key] = int(json.loads(r.stdout)["pageCount"])
            except (ValueError, KeyError, TypeError):
                raise RunError(f"쪽 수를 알 수 없다: {r.stderr.strip()[:300]}") from None
        return self._pages_cache[key]

    def _pdf_pages(self, doc: Path, out: Path, spec: str, cwd) -> subprocess.CompletedProcess:
        """쪽마다 export-pdf -p 로 그려 qpdf 로 잇는다 (PDF_PAGE_WORKERS 개씩 동시에)."""
        pages = parse_pages(spec, self.page_count(doc))
        tmp = Path(tempfile.mkdtemp(prefix="pdfpages-", dir=cwd or None))
        extra = [a for a in PDF_FONT_ARGS]

        def one(p: int) -> Path:
            f = tmp / f"p{p:05d}.pdf"
            rr = subprocess.run([self.rhwp, "export-pdf", str(doc), "-o", str(f), "-p", str(p - 1), *extra],
                                capture_output=True, text=True, timeout=self.timeout)
            if rr.returncode != 0 or not f.is_file():
                note_failure(rr.returncode, rr.stderr, "export-pdf-page")
                raise RunError(f"{p}쪽 PDF 실패 (exit {rr.returncode}): {rr.stderr.strip()[-300:]}")
            return f

        try:
            with ThreadPoolExecutor(max_workers=max(1, PDF_PAGE_WORKERS)) as pool:
                files = list(pool.map(one, pages))
            rq = subprocess.run(["qpdf", "--empty", "--pages", *map(str, files), "--", str(out)],
                                capture_output=True, text=True, timeout=self.timeout)
            if rq.returncode not in (0, 3) or not out.is_file():          # 3 = 경고만
                raise RunError(f"PDF 잇기 실패: {rq.stderr.strip()[-300:]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        body = {"format": "pdf", "output": str(out), "pages": pages, "pageCount": len(pages),
                "bytes": out.stat().st_size, "method": "쪽마다 export-pdf -p 후 qpdf 로 이음"}
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(body, ensure_ascii=False), stderr="")

    def _svg(self, doc: Path, page: int) -> Path:
        """한 쪽만 SVG 로 (export-svg -p). 예전엔 문서 전체를 그려 490쪽 문서가 /tmp 를 채우고(No space left)
        쪽 하나 보는 데도 전부 기다렸다. 쪽마다 기억하고, 문서는 최근 12개만 남긴다."""
        mt = doc.stat().st_mtime
        key = (str(doc), mt, page)
        with self._render_lock:
            hit = self._svg_cache.get(key)
            if hit and hit.is_file():
                return hit
            dkey = (str(doc), mt)
            td = self._svg_dirs.get(dkey)
            if td is None or not td.is_dir():
                td = Path(tempfile.mkdtemp(prefix="svg-"))
                self._svg_dirs[dkey] = td
                while len(self._svg_dirs) > 12:
                    old_key = next(iter(self._svg_dirs))
                    shutil.rmtree(self._svg_dirs.pop(old_key), ignore_errors=True)
                    for k in [k for k in self._svg_cache if k[:2] == old_key]:
                        self._svg_cache.pop(k)
            out = td / f"p{page}"
            out.mkdir(exist_ok=True)
            r = subprocess.run([self.rhwp, "export-svg", str(doc), "-p", str(page), "-o", str(out), "--json"],
                               capture_output=True, text=True, timeout=self.timeout)
            note_failure(r.returncode, r.stderr, "preview")
            try:
                man = json.loads(r.stdout)
                svg = out / Path(man["pages"][0]["path"]).name
            except (ValueError, KeyError, IndexError, TypeError):
                raise RunError(f"SVG 로 그리지 못했다: {r.stderr.strip()[:500]}") from None
            if not svg.is_file():
                hits = sorted(out.glob("*.svg"))
                if not hits:
                    raise RunError(f"SVG 로 그리지 못했다: {r.stderr.strip()[:500]}")
                svg = hits[0]
            self._svg_cache[key] = svg
            return svg

    def page_png(self, doc: Path, page: int, width: int = 900) -> tuple[bytes, int]:
        """한 쪽 PNG 와 전체 쪽 수 (쪽은 0부터, 범위 밖이면 가장 가까운 쪽)."""
        total = self.page_count(doc)
        if not total:
            raise RunError("그릴 쪽이 없다.")
        page = min(max(page, 0), total - 1)
        key = (str(doc), doc.stat().st_mtime, page, width)
        if key in self._png_cache:
            return self._png_cache[key], total
        conv = shutil.which("rsvg-convert")
        if not conv:
            raise RunError("이 서버에 rsvg-convert 가 없어 미리보기를 만들 수 없다.")
        with tempfile.TemporaryDirectory(prefix="pv-") as td:
            png = Path(td) / "p.png"
            rr = subprocess.run([conv, "-w", str(width), "-b", "white", "-o", str(png), str(self._svg(doc, page))],
                                capture_output=True, text=True, timeout=60)
            if rr.returncode != 0:
                note_failure(rr.returncode, rr.stderr, "preview-png")
                raise RunError(f"PNG 변환 실패: {rr.stderr.strip()[:300]}")
            data = png.read_bytes()
        if len(self._png_cache) > 96:
            self._png_cache.pop(next(iter(self._png_cache)))
        self._png_cache[key] = data
        return data, total

    def preview(self, doc: Path, pages: list[int], width: int = 900) -> list[tuple[int, bytes]]:
        """쪽을 PNG 로 — Claude 가 결과를 눈으로 확인한다 (rhwp SVG → rsvg-convert). 쪽은 0부터."""
        total = self.page_count(doc)
        out = []
        for pg in pages:
            if not 0 <= pg < total:
                raise RunError(f"{pg + 1}쪽이 없다 (전체 {total}쪽).")
            out.append((pg, self.page_png(doc, pg, width)[0]))
        return out

    def run(self, who, name: str, args: dict[str, Any], job: Job | None, post=None) -> dict[str, Any]:
        if name in CREATE_TOOLS:
            raise RunError(f"{name} 은(는) 원본 없이 새 문서를 만드는 기능이다 — hwp_new(name, spec) 로 부를 것 "
                           "(명세를 그대로 spec 에 넣으면 새 작업의 v0 이 된다).")
        t = self.m.get(name)
        args = {k: v for k, v in (args or {}).items() if v is not None}
        unknown = set(args) - set(t.props)
        if unknown:
            raise RunError(f"{name} 이(가) 모르는 인자: {sorted(unknown)}. 받는 것: {sorted(t.props)}")
        edits_doc = not t.read_only and "path" in t.props
        if edits_doc and isinstance(args.get("path"), str) and self._nas(who, args["path"]) is not None:
            raise RunError("원본은 고치지 않는다 — args.path 대신 doc 에 그 문서 경로를 주면 뒤에서 사본을 만들어 고친다.")
        if "path" in t.props and "path" not in args and job is not None:
            args["path"] = "current"
        if name == "hwp_export_pdf" and args.get("pages") and "output" not in args:
            stem = Path(job.name).stem if job else "문서"
            args["output"] = f"{stem}_{re.sub(r'[^0-9,-]', '', str(args['pages'])).replace(',', '_')}쪽.pdf"
        missing = [k for k in t.required if k not in args and k != "paths"]
        if missing:
            raise RunError(f"{name} 에 필요한 인자가 빠졌다: {missing}")

        real: dict[str, Any] = {}
        stdin_lines: list[str] = []
        for k, v in args.items():
            if k == "password":
                continue
            if k in DOC_IN or k in FILE_IN:
                real[k] = self._input(who, job, v, k)
            elif k in OUT:
                real[k] = self._output(job, v, k)
            elif k == "paths":
                stdin_lines = self._expand(who, job, v if isinstance(v, list) else [v])
            elif k == "plan":
                plan = dict(v)
                if "input" in plan:
                    plan["input"] = self._input(who, job, plan["input"], "plan.input")
                if "output" in plan:
                    plan["output"] = self._output(job, plan["output"], "plan.output")
                real[k] = plan
            else:
                if isinstance(v, str) and v.startswith("@"):
                    raise RunError(f"{k}: '@파일' 꼴은 받지 않는다 (서버 파일을 읽게 되므로) — 값을 직접 줄 것.")
                real[k] = v
        if "inPlace" in t.props and real.get("inPlace"):
            real["inPlace"] = False       # 판은 늘 새로 만든다 — 앞 판을 덮어쓰지 않는다
        dry = bool(real.get("dryRun"))

        base_args = [_fill(a, real) for a in t.cli["args"]]
        opts: list[str] = []
        for opt in t.cli.get("optionalArgs", []):
            w = opt.get("when")
            if w in real and real[w] not in (False, None):
                opts += [_fill(a, real) for a in opt["args"]]
        # `--` 뒤는 위치 인자다 (예: search <파일> --json -- <검색어>) — 선택 인자는 반드시 그 앞에
        if "--" in base_args:
            i = base_args.index("--")
            cmd = [self.rhwp] + base_args[:i] + opts + base_args[i:]
        else:
            cmd = [self.rhwp] + base_args + opts
        if PDF_FONT_ARGS and t.cli.get("command") == "export-pdf" and not any(a.startswith("--fallback-") for a in cmd):
            # rhwp 의 기본 예비 글꼴(Noto CJK)이 이 서버에 없어 문서 글꼴(함초롬바탕·맑은 고딕 등)이 한글 없는 DejaVu 로
            # 바뀌고 PDF 에 글자가 하나도 안 남았다 (2026-10-14). 서버에 있는 나눔 글꼴을 예비로 준다
            cmd += PDF_FONT_ARGS
        stdin = None
        pw = t.cli.get("passwordStdin")
        if pw and args.get("password"):
            cmd.append(pw["flag"])
            stdin = str(args["password"]).splitlines()[0] + "\n"
        if name in self.m.stdin_tools:
            if not stdin_lines:
                raise RunError("paths 가 비어 있다 — 문서 폴더·파일 경로 목록을 줄 것.")
            stdin = "\n".join(stdin_lines) + "\n"

        cwd = job.dir if job else None
        pages_spec = real.pop("pages", None) if name == "hwp_export_pdf" else None
        before = _listing(job) if job else set()
        started = time.time()
        if job and edits_doc and not dry:
            self.sb.busy(job, True, name)
        try:
            if pages_spec:
                r = self._pdf_pages(Path(real["path"]), Path(real["output"]), str(pages_spec), cwd)
            else:
                r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=self.timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            note_failure("timeout", "", name)
            raise RunError(f"{self.timeout}초 안에 끝나지 않았다.") from None
        finally:
            if job:
                self.sb.busy(job, False)
        log.info("%s %s exit=%s %.1fs", who.label, name, r.returncode, time.time() - started)
        note_failure(r.returncode, r.stderr, name)

        result: dict[str, Any] = {"tool": name, "exit": r.returncode}
        if job:
            note = ", ".join(f"{k}={_short(v)}" for k, v in args.items() if k not in DOC_IN | OUT | {"password"})
            result.update(self._collect(job, name, before, edits_doc and not dry, note))
        body = r.stdout.strip()
        if r.returncode in (1, 2) and not body:
            raise RunError(f"rhwp 실패 (exit {r.returncode}): {self._mask(r.stderr.strip(), job)[:2000]}")
        try:
            parsed: Any = json.loads(body) if body else None
        except ValueError:
            parsed = [json.loads(x) for x in body.splitlines() if x.strip().startswith("{")] or body
        if post and not isinstance(parsed, str):
            parsed = post(parsed)          # 좁히기(표 번호 등)는 자르기 전에 — 잘린 문자열은 좁힐 수 없다
        text = self._mask(json.dumps(parsed, ensure_ascii=False) if not isinstance(parsed, str) else parsed, job)
        if len(text) > MAX_RETURN:
            result["result_truncated"] = True
            if job:
                full = job.out / f"{name}-{time.strftime('%H%M%S')}.json"
                full.write_text(text, encoding="utf-8")
                result["full_result"] = f"out/{full.name}"
            else:                          # 작업 없이 원본을 바로 읽은 경우 — 둘 곳이 없으니 자르고 안내만
                result["note"] = (f"결과가 {len(text):,}자라 앞 {MAX_RETURN:,}자만 보낸다. 좁혀서 다시 부르거나, "
                                  "version=\"v0\" 을 주면 사본이 생겨 전체가 out/ 파일로 남는다.")
            result["result"] = text[:MAX_RETURN]
        else:
            result["result"] = json.loads(text) if not isinstance(parsed, str) else text[:MAX_RETURN]
        if result.get("new_version") and isinstance(parsed, dict) and isinstance(parsed.get("changedPages"), list):
            job.versions[-1].pages = [int(x) + 1 for x in parsed["changedPages"][:50]]     # 1쪽부터
            self.sb.save_meta(job)
        if r.returncode == 3:
            result["note"] = "exit 3 = 검증에서 차이·불일치가 나왔다 (오류가 아니라 결과다). result 를 볼 것."
        if r.stderr.strip() and r.returncode != 0:
            result["stderr"] = self._mask(r.stderr.strip(), job)[:1500]
        return result

    def _collect(self, job: Job, name: str, before: set[str], versioned: bool, note: str = "") -> dict[str, Any]:
        """새로 생긴 파일: 문서면 다음 판으로(편집 기능일 때), 나머지는 out/ 으로."""
        new = sorted((job.dir / p for p in _listing(job) - before), key=lambda p: p.stat().st_mtime)
        docs = [p for p in new if p.parent == job.dir and p.suffix.lower() in DOC_EXT]
        out: dict[str, Any] = {}
        if versioned and len(docs) == 1:
            v = self.sb.commit(job, docs[0], by=name, note=note)
            out["new_version"] = f"v{v.v}"
            docs = []
        for p in new:
            if p.parent == job.dir and (p in docs or p.suffix.lower() not in DOC_EXT):
                target = job.out / p.name
                if target.exists():
                    target = job.out / f"{p.stem}-{time.strftime('%H%M%S')}{p.suffix}"
                shutil.move(str(p), target)
        made = sorted({f"out/{p.name}" for p in job.out.iterdir()} - {x for x in before if x.startswith("out/")})
        if made:
            out["files"] = made
        return out

    def _mask(self, s: str, job: Job | None) -> str:
        """응답에 서버 실제 경로가 새지 않게 가상 경로로 바꾼다."""
        if job:
            s = s.replace(str(job.out) + "/", "out/").replace(str(job.dir) + "/", "")
        for name, base in self.roots.items():
            s = s.replace(str(base) + "/", f"{name}/").replace(str(base), name)
        return s.replace(str(self.sb.base) + "/", "sandbox/")


def _short(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return s if len(s) <= 60 else s[:57] + "…"


def _listing(job: Job) -> set[str]:
    out = {p.name for p in job.dir.iterdir() if p.is_file() and p.name != "job.json" and not p.name.startswith(".")}
    return out | {f"out/{p.name}" for p in job.out.iterdir()}


def _fill(arg: str, vals: dict[str, Any]) -> str:
    def sub(m: re.Match) -> str:
        k = m.group(1)
        if k not in vals:
            raise RunError(f"인자 {k!r} 가 필요하다.")
        v = vals[k]
        if isinstance(v, bool):
            return "true" if v else "false"
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return re.sub(r"\{([A-Za-z0-9_]+)\}", sub, arg)
