"""채팅 안 화면(MCP Apps)과 첨부 파일 올리기 — rhwp 없이 (가짜 rhwp) 돈다.

  python tests/test_apps.py

확인하는 것: 편집·미리보기 도구에 _meta.ui.resourceUri 가 붙는다 · ui:// 화면이 mcp-app MIME·CSP 로 나온다 ·
앱 전용 도구는 visibility=app · 올리기 링크(multipart·본문 그대로)가 그 사람의 새 작업이 된다 · 한글 아닌 것·틀린 코드는 막는다.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

FAKE_RHWP = """#!/bin/sh
if [ "$1" = "capabilities" ]; then echo '{"tools": [], "invocation": {}}'; exit 0; fi
if [ "$1" = "--version" ]; then echo 'rhwp 0.0.0'; exit 0; fi
exit 1
"""
HWPX = b"PK\x03\x04" + b"\x00" * 64


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="hwpapps-"))
    (tmp / "DOCS").mkdir()
    rhwp = tmp / "rhwp"
    rhwp.write_text(FAKE_RHWP)
    rhwp.chmod(0o755)
    tok = secrets.token_urlsafe(32)
    launcher = (
        "import logging\n"
        "from hwp_cowork.config import Config\n"
        "from hwp_cowork.auth import Principal, Registry\n"
        "from hwp_cowork.server import serve\n"
        "logging.basicConfig(level=logging.WARNING)\n"
        f"reg = Registry({{}}, None, {{{tok!r}: Principal('Bob', 'j@x', frozenset({{'DOCS'}}), False, 'bob')}})\n"
        "serve(Config.from_env(), reg, None)\n")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "HWPCOWORK_ROOTS": f"DOCS={tmp / 'DOCS'}", "HWPCOWORK_TRANSPORT": "http", "HWPCOWORK_HOST": "127.0.0.1",
           "HWPCOWORK_PORT": str(port), "HWPCOWORK_PRINCIPALS_FILE": "unused", "HWPCOWORK_WORK_DIR": str(tmp / "work"),
           "HWPCOWORK_DATA_DIR": str(tmp / "data"), "HWPCOWORK_RHWP_BIN": str(rhwp), "HWPCOWORK_PUBLIC_BASE": base,
           "HWPCOWORK_UPLOAD_BASE": base}
    srv = subprocess.Popen([sys.executable, "-c", launcher], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           text=True)
    try:
        for _ in range(100):
            try:
                if urllib.request.urlopen(base + "/healthz", timeout=1).read() == b"ok":
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise SystemExit("서버가 뜨지 않았다:\n" + srv.stderr.read())
        client = httpx.AsyncClient(headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        async with streamable_http_client(base + "/mcp", http_client=client) as (rd, wr, *_):
            async with ClientSession(rd, wr) as s:
                await s.initialize()

                tools = {t.name: t for t in (await s.list_tools()).tools}
                for name in ("hwp_new", "hwp_replace_text", "hwp_set_cell", "hwp_run", "hwp_upload_link"):
                    assert (tools[name].meta or {}).get("ui", {}).get("resourceUri") == "ui://hwp/viewer.html", name
                for name in ("hwp_app_page", "hwp_app_state", "hwp_app_editor", "hwp_app_uploaded"):
                    assert tools[name].meta["ui"]["visibility"] == ["app"], name
                assert "ui" not in (tools["hwp_info"].meta or {}) and "ui" not in (tools["hwp_preview"].meta or {})

                res = await s.read_resource("ui://hwp/viewer.html")
                c = res.contents[0]
                assert c.mime_type == "text/html;profile=mcp-app", c.mime_type
                assert "ui/initialize" in c.text and "hwp_app_page" in c.text
                assert base in c.meta["ui"]["csp"]["connectDomains"], c.meta

                r = await s.call_tool("hwp_upload_link", {})
                link = r.structured_content or json.loads(r.content[0].text)
                url = link["upload_url"]
                assert url.startswith(base + "/hu/") and "curl" in link, link

                async with httpx.AsyncClient() as h:
                    # 코드 실행의 curl -F (multipart)
                    a = (await h.post(url, files={"file": ("메모.hwpx", HWPX)})).json()
                    assert a["name"] == "메모.hwpx" and len(a["job"]) == 6 and "압축" in a["for_claude"], a
                    # claude.ai 가 한글을 밑줄로 바꾼 첨부 이름은 걷어내고, name 필드가 있으면 그 이름
                    m = (await h.post(url, files={"file": ("d9ddcbd2-1791292445008______________260626.hwpx", HWPX)})).json()
                    assert m["name"] == "260626.hwpx", m
                    n = (await h.post(url, files={"file": ("x_____.hwpx", HWPX)}, data={"name": "메모보고 양식.hwpx"})).json()
                    assert n["name"] == "메모보고 양식.hwpx", n
                    # 본문 그대로 + ?name=
                    b = (await h.put(url, params={"name": "보고.hwp"}, content=b"\xd0\xcf\x11\xe0" + b"\x00" * 32)).json()
                    assert b["name"] == "보고.hwp", b
                    # 채팅 안 업로드 칸(fetch) 의 CORS 사전 요청
                    o = await h.options(url, headers={"Origin": "https://x.claudeusercontent.com",
                                                      "Access-Control-Request-Method": "POST"})
                    assert o.status_code == 204 and o.headers["access-control-allow-origin"] == "*"
                    assert (await h.get(url)).text.count("한글 파일 올리기") >= 1
                    bad = await h.post(url, files={"file": ("x.exe", b"MZ")})
                    assert bad.status_code == 400, bad.text
                    fake = await h.post(url, files={"file": ("x.hwpx", b"hello")})
                    assert fake.status_code == 400 and "보이지 않는다" in fake.json()["error"]
                    wrong = await h.post(base + "/hu/AAAAAAAA", files={"file": ("x.hwpx", HWPX)})
                    assert wrong.status_code == 403

                jobs = (await s.call_tool("hwp_jobs", {})).structured_content or {}
                names = {j["name"] for j in jobs.get("jobs", jobs.get("result", {}).get("jobs", []))} if jobs else set()
                assert {"메모.hwpx", "보고.hwp"} <= names, jobs
                upd = await s.call_tool("hwp_app_uploaded", {"upload_url": url})
                upd = upd.structured_content or json.loads(upd.content[0].text)
                assert upd["job"] and upd["count"] == 4, upd
                none = await s.call_tool("hwp_app_uploaded", {"upload_url": base + "/hu/AAAAAAAA"})
                assert (none.structured_content or json.loads(none.content[0].text))["job"] is None
                st = await s.call_tool("hwp_app_state", {"job": a["job"]})
                stc = st.structured_content or json.loads(st.content[0].text)
                assert stc["job"] == a["job"] and stc["current"] == "v0" and stc["rev"], stc
        print("ok")
    finally:
        srv.terminate()


if __name__ == "__main__":
    asyncio.run(main())
