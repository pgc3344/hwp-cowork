"""종단 테스트 — HTTP 서버를 띄우고 두 사람이 붙는다. 실제 rhwp 와 샘플 한글 파일이 필요하다.

  HWPCOWORK_RHWP_BIN=/path/to/rhwp HWPCOWORK_TEST_HWP=sample.hwp python tests/test_e2e.py

확인하는 것: NAS 경로를 그대로 쓰면 뒤에서 사본이 생기고 판이 쌓인다 · NAS 원본은 한 바이트도 안 바뀐다 ·
어떤 인자로도 샌드박스 밖을 못 건드린다 · 다른 사람 작업·루트는 안 보인다 · 에디터 저장·NAS 사본 저장 경로.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

RHWP = os.environ.get("HWPCOWORK_RHWP_BIN", "rhwp")
SAMPLE = os.environ.get("HWPCOWORK_TEST_HWP")


def ok(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def err(result) -> str:
    assert result.is_error, "오류여야 한다: " + result.content[0].text[:300]
    return result.content[0].text


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def session(url: str, token: str):
    return streamable_http_client(url, http_client=httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=120))


class FakeNas(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeNas.got.append((self.path, self.headers.get("X-Internal-Key"), len(body)))
        out = json.dumps({"path": "DOCS/예산/샘플_v1.hwp", "bytes": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


async def main() -> None:
    assert SAMPLE and Path(SAMPLE).is_file(), "HWPCOWORK_TEST_HWP 에 샘플 .hwp 가 필요하다"
    tmp = Path(tempfile.mkdtemp(prefix="hwp-cowork-"))
    bob, alice = tmp / "DOCS", tmp / "PRIV"
    (bob / "예산").mkdir(parents=True)
    (alice / "비밀").mkdir(parents=True)
    doc = bob / "예산" / ("샘플" + Path(SAMPLE).suffix)
    shutil.copy(SAMPLE, doc)
    shutil.copy(SAMPLE, alice / "비밀" / doc.name)
    (bob / "예산" / "값.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    nas_hash = hashlib.sha256(doc.read_bytes()).hexdigest()
    vdoc = f"DOCS/예산/{doc.name}"

    nas = HTTPServer(("127.0.0.1", 0), FakeNas)
    threading.Thread(target=nas.serve_forever, daemon=True).start()

    alice_tok, bob_tok = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    launcher = (
        "import logging\n"
        "from hwp_cowork.config import Config\n"
        "from hwp_cowork.auth import Principal, Registry\n"
        "from hwp_cowork.server import serve\n"
        "logging.basicConfig(level=logging.WARNING)\n"
        f"reg = Registry({{}}, None, {{{alice_tok!r}: Principal('Alice', 'g@x', frozenset({{'PRIV', 'DOCS'}}), True, 'alice'),\n"
        f"                         {bob_tok!r}: Principal('Bob', 'j@x', frozenset({{'DOCS'}}), False, 'bob')}})\n"
        "serve(Config.from_env(), reg, None)\n")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "HWPCOWORK_ROOTS": f"DOCS={bob},PRIV={alice}", "HWPCOWORK_TRANSPORT": "http", "HWPCOWORK_HOST": "127.0.0.1",
           "HWPCOWORK_PORT": str(port), "HWPCOWORK_PRINCIPALS_FILE": "unused", "HWPCOWORK_WORK_DIR": str(tmp / "work"),
           "HWPCOWORK_DATA_DIR": str(tmp / "data"), "HWPCOWORK_RHWP_BIN": RHWP, "HWPCOWORK_PUBLIC_BASE": base,
           "HWPCOWORK_NAS_SAVE_URL": f"http://127.0.0.1:{nas.server_port}/internal/save-copy",
           "HWPCOWORK_NAS_INTERNAL_KEY": "k" * 40}
    srv = subprocess.Popen([sys.executable, "-c", launcher], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    url = base + "/mcp"
    try:
        for _ in range(100):
            try:
                if urllib.request.urlopen(base + "/healthz", timeout=1).read() == b"ok":
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise SystemExit("서버가 뜨지 않았다:\n" + srv.stderr.read())

        async with session(url, alice_tok) as (rd, wr, *_):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert "hwp_open" not in names and {"hwp_run", "hwp_catalog", "hwp_set_cell"} <= names, names

                # 읽기는 NAS 경로 그대로 — 사본을 만들지 않는다
                info = ok(await s.call_tool("hwp_info", {"doc": vdoc}))
                assert "job" not in info and info["result"]["pageCount"] >= 1, info
                assert ok(await s.call_tool("hwp_jobs", {}))["jobs"] == []
                tabs = ok(await s.call_tool("hwp_tables", {"doc": vdoc}))["result"]["tables"]
                t = next(t for t in tabs if t["rows"] >= 2 and t["cols"] >= 2)
                cell = next(c for c in t["cells"] if c["row"] >= 1)

                # 편집 — 뒤에서 사본(작업)이 생기고 v1, v2 로 쌓인다
                e1 = ok(await s.call_tool("hwp_set_cell", {"doc": vdoc, "table": t["index"], "row": cell["row"],
                                                          "col": cell["col"], "text": "테스트값123"}))
                assert e1["new_version"] == "v1" and e1["current"] == "v1", e1
                job = e1["job"]
                e2 = ok(await s.call_tool("hwp_replace_text", {"doc": vdoc, "find": "테스트값123", "replace": "바뀐값456"}))
                assert e2["job"] == job and e2["new_version"] == "v2", e2
                assert len(ok(await s.call_tool("hwp_jobs", {}))["jobs"]) == 1
                # 같은 NAS 경로로 읽으면 최신 판
                loc = ok(await s.call_tool("hwp_locate", {"doc": vdoc, "query": "바뀐값456", "context": 2}))
                assert loc["job"] == job and "바뀐값456" in json.dumps(loc["result"], ensure_ascii=False), loc
                d = ok(await s.call_tool("hwp_diff", {"doc": job}))
                assert d["result"]["identical"] is False, d
                assert any(c["new"] and "바뀐값456" in c["new"] for c in d["changes"]), d    # 무엇이 무엇으로
                u = ok(await s.call_tool("hwp_undo", {"doc": vdoc}))
                assert u["removed"] == "v2" and u["current"] == "v1"
                # 드라이런은 판을 만들지 않는다
                dr = ok(await s.call_tool("hwp_replace_text", {"doc": job, "find": "테스트값123", "replace": "x", "dry_run": True}))
                assert "new_version" not in dr and dr["current"] == "v1", dr

                # 새 문서 — 원본 없이 만들고, 그 job 으로 이어서 고친다. scaffold 를 hwp_run 으로 부르면 hwp_new 로 안내
                nw = ok(await s.call_tool("hwp_new", {"name": "보고서", "spec": {"version": "1", "title": "보고서", "blocks": [
                    {"type": "heading", "level": 1, "text": "1. 개요"}, {"type": "paragraph", "text": "합계 1,000원"},
                    {"type": "table", "rows": [["구분", "금액"], ["가", "1,000"]]}]}}))
                assert nw["current"] == "v0" and nw["name"] == "보고서.hwpx", nw
                ne = ok(await s.call_tool("hwp_replace_text", {"doc": nw["job"], "find": "합계 1,000원", "replace": "합계 2,000원"}))
                assert ne["new_version"] == "v1", ne
                # 서식 명세 — 표지 띠·목차·런 서식·병합·음영 표·상자, HWP5 로
                rich = ok(await s.call_tool("hwp_new", {"name": "공문.hwp", "format": "hwp", "spec": {
                    "page": {"header": "머리말", "page_number": "- n -"},
                    "blocks": [{"rule": "#4472C4"}, {"p": "추진계획(안)", "style": "title"}, {"page_break": True},
                               {"toc": [["Ⅰ. 개요", "1"]]}, {"box": "요약", "fill": "#EEF3FA"},
                               {"p": [{"t": "(굵게)", "b": True, "color": "#1F4E9A"}, {"t": " 본문"}], "style": "l1"},
                               {"table": [["구분", "1분기", ""], ["", "계획", "실적"], ["예산", "1", "2"]], "header": 2,
                                "merge": [[0, 0, 2, 1], [0, 1, 1, 2]], "fill": {"2,2": "#FFF2CC"}, "border": "rows"},
                               {"p": "※ 주석", "style": "note"}]}}))
                assert rich["name"] == "공문.hwp" and rich["result"]["format"] == "hwp", rich
                pv = await s.call_tool("hwp_preview", {"doc": rich["job"], "pages": "1-2"})
                assert not pv.is_error and [c.type for c in pv.content] == ["text", "image", "image"], pv.content[:1]
                assert "1~6" in err(await s.call_tool("hwp_preview", {"doc": rich["job"], "pages": "1-9"}))
                auto = ok(await s.call_tool("hwp_new", {"name": "목차", "spec": {"blocks": [
                    {"rule": "#1F3864"}, {"p": "표지", "style": "title"}, {"toc": ["Ⅰ. 개요", "Ⅱ. 계획"]},
                    {"p": "개요", "style": "h1", "num": "Ⅰ", "page_break": True, "page_restart": 1},
                    {"p": "계획", "style": "h1", "num": "Ⅱ", "page_break": True}]}}))
                assert auto["result"]["toc_pages"] == {"Ⅰ. 개요": "1", "Ⅱ. 계획": "2"}, auto
                tl = ok(await s.call_tool("hwp_templates", {}))
                assert {t["name"] for t in tl["templates"]} >= {"보고서", "기안문", "메모보고"} and "□" in tl["rules"], tl
                for tn in ("보고서", "기안문", "메모보고"):
                    sp = ok(await s.call_tool("hwp_templates", {"name": tn}))["spec"]
                    made = ok(await s.call_tool("hwp_new", {"name": tn, "spec": sp}))
                    assert made["result"]["pages"] >= 1, made
                assert "없다" in err(await s.call_tool("hwp_templates", {"name": "없는것"}))
                tx = ok(await s.call_tool("hwp_text", {"doc": rich["job"]}))
                assert "추진계획(안)" in json.dumps(tx, ensure_ascii=False), tx
                assert "hwp_new" in err(await s.call_tool("hwp_run", {"tool": "hwp_scaffold", "args": {"path": "x.json"}}))
                assert "spec 오류" in err(await s.call_tool("hwp_new", {"name": "x", "spec": {"bad": 1}}))
                assert "merge" in err(await s.call_tool("hwp_new", {"name": "x", "spec": {"blocks": [
                    {"table": [["a"]], "merge": [[0, 0, 3, 3]]}]}}))
                assert "이상하다" in err(await s.call_tool("hwp_new", {"name": "../x", "spec": {}}))

                # 전체 기능 — catalog → run
                cat = ok(await s.call_tool("hwp_catalog", {"query": "PDF"}))
                assert any(x["name"] == "hwp_export_pdf" for x in cat["tools"]), cat
                assert ok(await s.call_tool("hwp_catalog", {}))["count"] > 100
                pdf = ok(await s.call_tool("hwp_run", {"tool": "hwp_export_pdf", "doc": job,
                                                       "args": {"output": "../../../../tmp/escape.pdf"}}))
                assert pdf["files"] == ["out/escape.pdf"] and not Path("/tmp/escape.pdf").exists(), pdf
                assert (tmp / "work" / "alice" / job / "out" / "escape.pdf").is_file()
                # 일부 쪽 PDF — 쪽마다 그려 잇는다 (split 은 재조판돼 쪽이 달라진다)
                if shutil.which("qpdf"):
                    part = ok(await s.call_tool("hwp_run", {"tool": "hwp_export_pdf", "doc": job, "args": {"pages": "1"}}))
                    assert part["result"]["pageCount"] == 1 and part["files"], part
                    assert "문서 밖" in err(await s.call_tool("hwp_run", {"tool": "hwp_export_pdf", "doc": job,
                                                                         "args": {"pages": "1-9999"}}))
                bs = ok(await s.call_tool("hwp_run", {"tool": "hwp_batch_search", "args": {"paths": ["DOCS/예산"], "query": "제"}}))
                assert bs["exit"] in (0, 1) and "DOCS/예산/" in json.dumps(bs["result"], ensure_ascii=False), bs
                assert str(bob) not in json.dumps(bs, ensure_ascii=False), "실제 경로가 새면 안 된다"

                # 샌드박스 밖으로는 못 나간다
                err(await s.call_tool("hwp_run", {"tool": "hwp_info", "args": {"path": "/etc/passwd"}}))
                err(await s.call_tool("hwp_run", {"tool": "hwp_info", "doc": job, "args": {"path": "out/../../x"}}))
                err(await s.call_tool("hwp_run", {"tool": "hwp_set_cell", "args": {"path": vdoc, "table": 0, "row": 0, "col": 0, "text": "x"}}))
                err(await s.call_tool("hwp_run", {"tool": "hwp_fill_fields", "doc": job, "args": {"data": "@/etc/passwd"}}))
                err(await s.call_tool("hwp_run", {"tool": "hwp_nope"}))
                err(await s.call_tool("hwp_info", {"doc": "DOCS/../PRIV/비밀/" + doc.name}))

                # 실시간 화면 — 편집 결과에 링크가 오고, 화면에서 저장·자동 저장·충돌·되돌리기·비교·NAS 적용
                assert e1["live_view"].startswith(base + "/live/") and "실시간으로 보기" in e1["tell_user"]
                v = ok(await s.call_tool("hwp_view_link", {"doc": job}))
                assert v["url"] == e1["live_view"], "작업마다 같은 링크를 재사용"
                L = v["url"].replace(base, "")
                assert httpx.get(base + L).status_code == 200
                st = httpx.get(f"{base}{L}/state").json()
                assert st["current"] == "v1" and [x["v"] for x in st["versions"]] == ["v0", "v1"] and st["can_apply"], st
                assert st["ext"] in ("hwp", "hwpx"), st
                assert st["versions"][1]["by"] == "hwp_set_cell" and "테스트값123" in st["versions"][1]["note"], st
                assert st["versions"][1]["pages"] and all(isinstance(x, int) and x >= 1 for x in st["versions"][1]["pages"]), st
                with httpx.stream("GET", f"{base}{L}/events", timeout=10) as ev:
                    for line in ev.iter_lines():
                        if line.startswith("data:"):
                            assert json.loads(line[5:])["current"] == "v1"
                            break
                raw = httpx.get(f"{base}{L}/file?v=v1").content
                r = httpx.post(f"{base}{L}/save?auto=1&base=v1", content=raw)
                assert r.json()["version"] == "v2", r.text
                r = httpx.post(f"{base}{L}/save?auto=1&base=v2", content=raw)
                assert r.json()["version"] == "v2", "연속 자동 저장은 한 판으로 묶는다"
                assert httpx.post(f"{base}{L}/save?auto=0&base=v1", content=raw).status_code == 409, "옛 판 위 저장은 충돌"
                assert httpx.post(f"{base}{L}/save?auto=0&base=v2", content=b"x" * 100).status_code == 400
                r = httpx.post(f"{base}{L}/restore?v=v0").json()
                assert r["version"] == "v3", r
                d = httpx.get(f"{base}{L}/diff?a=v1&b=v3").json()
                assert d["count"] >= 1 and any("테스트값" in (c["old"] or "") for c in d["changes"]), d
                assert httpx.get(f"{base}{L}/diff?a=v0&b=v3").json()["count"] == 0
                # NAS 에 저장 — 폴더를 골라 이름을 정한다. 원본 덮어쓰기 길은 없다
                fo = httpx.get(f"{base}{L}/folders").json()
                assert sorted(fo["folders"]) == ["DOCS", "PRIV"] and fo["default"] == "DOCS/예산", fo
                assert httpx.get(f"{base}{L}/folders?path=PRIV").json()["folders"] == ["비밀"]
                assert httpx.get(f"{base}{L}/folders?path=DOCS/../PRIV").status_code in (400, 403)
                ap = httpx.post(f"{base}{L}/apply?v=v1").json()
                assert ap["mode"] == "copy" and ap["version"] == "v1" and "folder=DOCS" in FakeNas.got[-1][0], (ap, FakeNas.got[-1])
                ap = httpx.post(f"{base}{L}/apply?v=v1&folder=PRIV/비밀&name=최종.hwp&mode=replace").json()
                q = urllib.parse.unquote(FakeNas.got[-1][0])
                assert ap["mode"] == "copy" and "folder=PRIV/비밀" in q and "name=최종.hwp" in q and "replace" not in q, q
                assert httpx.post(f"{base}{L}/apply?v=v1&name=../x.hwp").status_code == 400
                st = httpx.get(f"{base}{L}/state").json()
                assert [a["mode"] for a in st["applied"]] == ["copy", "copy"], st
                assert httpx.get(f"{base}/live/ZZZZZZZZ/state").status_code == 403

                # Claude 는 사람이 고친 판을 알게 된다 (v2 직접 편집 자동 저장, v3 되돌림)
                seen = ok(await s.call_tool("hwp_info", {"doc": job}))
                assert [h["version"] for h in seen["human_edits"]] == ["v2", "v3"], seen.get("human_edits")
                assert "human_edits" not in ok(await s.call_tool("hwp_info", {"doc": job})), "한 번 알린 건 다시 안 알린다"
                # 사람이 화면에서 저장 안 된 수정 중이면 Claude 편집은 기다렸다 멈춘다
                # (화면처럼 2초마다 "고치는 중" 을 보낸다)
                beating = threading.Event()

                def beat():
                    while not beating.is_set():
                        httpx.post(f"{base}{L}/presence?dirty=1&v=v3")
                        beating.wait(2)
                threading.Thread(target=beat, daemon=True).start()
                time.sleep(0.5)
                t0 = time.time()
                msg = err(await s.call_tool("hwp_replace_text", {"doc": job, "find": "제", "replace": "제"}))
                assert "직접 고치는 중" in msg and time.time() - t0 >= 10, (msg, time.time() - t0)
                beating.set()
                httpx.post(f"{base}{L}/presence?dirty=0&v=v3")
                ok(await s.call_tool("hwp_replace_text", {"doc": job, "find": "제", "replace": "제", "dry_run": True}))
                dl = ok(await s.call_tool("hwp_download_link", {"doc": job, "file": "out/escape.pdf"}))
                assert httpx.get(dl["url"]).content[:4] == b"%PDF"

                # 외부 저장 엔드포인트(HWPCOWORK_NAS_SAVE_URL)로 사본 저장
                sv = ok(await s.call_tool("hwp_save_copy", {"doc": job}))
                assert sv["saved_to"].endswith("_v1.hwp"), sv
                path, key, n = FakeNas.got[-1]
                assert key == "k" * 40 and "user=alice" in path and n > 1000, FakeNas.got

        async with session(url, bob_tok) as (rd, wr, *_):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                # 다른 사람 작업은 안 보인다, 볼 수 없는 루트, 쓰기 권한 없음
                assert ok(await s.call_tool("hwp_jobs", {}))["jobs"] == []
                assert "없다" in err(await s.call_tool("hwp_info", {"doc": job}))
                err(await s.call_tool("hwp_info", {"doc": f"PRIV/비밀/{doc.name}"}))
                e = ok(await s.call_tool("hwp_set_cell", {"doc": vdoc, "table": t["index"], "row": cell["row"],
                                                         "col": cell["col"], "text": "사람편집"}))
                assert e["job"] != job and e["new_version"] == "v1"
                assert "권한" in err(await s.call_tool("hwp_save_copy", {"doc": vdoc}))

        # NAS 원본은 그대로. 원본이 바뀌면 새 사본
        assert hashlib.sha256(doc.read_bytes()).hexdigest() == nas_hash, "NAS 원본이 바뀌면 안 된다"
        os.utime(doc, (time.time() + 60, time.time() + 60))
        async with session(url, alice_tok) as (rd, wr, *_):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                assert "job" not in ok(await s.call_tool("hwp_info", {"doc": vdoc})), "바뀐 원본은 옛 사본에 붙지 않는다"
        print("ok — 종단 테스트 통과")
    finally:
        srv.terminate()
        nas.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
