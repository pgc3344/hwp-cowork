"""회귀: 볼 수 없는 루트에는 hwp_save_copy 로도 쓸 수 없다 (GHSA — 쓰기 루트 권한 우회).

  HWPCOWORK_RHWP_BIN=/path/to/rhwp HWPCOWORK_TEST_HWP=sample.hwp pytest tests/test_save_authz.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

RHWP, SAMPLE = os.environ.get("HWPCOWORK_RHWP_BIN"), os.environ.get("HWPCOWORK_TEST_HWP")
pytestmark = pytest.mark.skipif(not (RHWP and SAMPLE), reason="rhwp 바이너리와 샘플 한글 파일이 필요하다")
TOK = "c" * 40


def test_cannot_save_into_invisible_root(tmp_path):
    (tmp_path / "docs").mkdir(); (tmp_path / "secret").mkdir()
    shutil.copy(SAMPLE, tmp_path / "docs" / "a.hwp")
    (tmp_path / "tokens.json").write_text(json.dumps([{"token": TOK, "label": "Carol", "roots": ["docs"], "write": True}]))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, "HWPCOWORK_ROOTS": f"docs={tmp_path / 'docs'},secret={tmp_path / 'secret'}",
           "HWPCOWORK_WRITABLE_ROOTS": "docs,secret", "HWPCOWORK_TRANSPORT": "http", "HWPCOWORK_PORT": str(port),
           "HWPCOWORK_AUTH": "token", "HWPCOWORK_TOKENS_FILE": str(tmp_path / "tokens.json"),
           "HWPCOWORK_WORK_DIR": str(tmp_path / "w"), "HWPCOWORK_DATA_DIR": str(tmp_path / "d"),
           "HWPCOWORK_RHWP_BIN": RHWP, "HWPCOWORK_PUBLIC_BASE": f"http://127.0.0.1:{port}"}
    srv = subprocess.Popen([sys.executable, "-m", "hwp_cowork.server"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.2)

        async def go():
            client = httpx.AsyncClient(headers={"Authorization": f"Bearer {TOK}"}, timeout=120)
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (rd, wr, *_):
                async with ClientSession(rd, wr) as c:
                    await c.initialize()
                    await c.call_tool("hwp_replace_text", {"doc": "docs/a.hwp", "find": "부", "replace": "部"})
                    bad = await c.call_tool("hwp_save_copy", {"doc": "docs/a.hwp", "folder": "secret", "name": "x.hwp"})
                    ok = await c.call_tool("hwp_save_copy", {"doc": "docs/a.hwp"})
                    return bad, ok
        bad, ok = asyncio.run(go())
        assert bad.is_error, bad
        assert not ok.is_error, ok
        assert list((tmp_path / "secret").iterdir()) == []
        assert "a_v1.hwp" in {p.name for p in (tmp_path / "docs").iterdir()}
    finally:
        srv.terminate()
