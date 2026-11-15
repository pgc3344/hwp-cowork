"""rhwp CLI 조립과 경로 인자 분류 — 실제 rhwp 없이."""

from hwp_cowork.runner import SERVER_BLOCKED, Manifest, Tool, optional_args

CELL_FMT = {"args": ["edit", "apply-char-format-in-cell", "{path}", "--json"],
            "optionalArgs": [{"args": ["--table", "{table}"], "when": "table"},
                             {"args": ["--row", "{row}"], "when": "row"},
                             {"args": ["--col", "{col}"], "when": "col"},
                             {"args": ["--color", "{color}"], "when": "color"},
                             {"args": ["--dry-run"], "when": "dryRun"}]}


def test_zero_is_a_value():
    # 첫 표·첫 행·첫 칸 — 예전에는 0 == False 라 셋 다 빠져 exit 2 사용법 오류
    opts = optional_args(CELL_FMT, {"table": 0, "row": 0, "col": 0, "color": "#000000"})
    assert opts == ["--table", "0", "--row", "0", "--col", "0", "--color", "#000000"]


def test_switches_and_missing():
    assert optional_args(CELL_FMT, {"table": 1, "dryRun": True}) == ["--table", "1", "--dry-run"]
    assert optional_args(CELL_FMT, {"table": 1, "dryRun": False, "row": None}) == ["--table", "1"]


def _tool(name, props):
    return Tool(name, "", {"properties": props}, {"args": []}, False, False)


def _check(name, props):
    return Manifest._check(object.__new__(Manifest), _tool(name, props))


def test_path_args_described_with_other_words_are_blocked():
    # '이름' 같은 낱말이 같이 있어도 '경로' 라고 적혔으면 경로다 — 모르는 것은 막는다
    props = {"path": {"type": "string"},
             "rows": {"type": "string", "description": "데이터 행 파일 경로. 첫 줄 헤더가 누름틀 이름"}}
    assert "분류되지 않은 경로 인자" in _check("hwp_somefuture", props)


def test_known_tool_path_args_pass_and_get_rewritten():
    props = {"path": {"type": "string"},
             "data": {"type": "string", "description": "데이터 행 파일 경로 … 누름틀 이름"},
             "nameField": {"type": "string", "description": "산출 파일 이름으로 쓸 데이터 필드 이름"}}
    assert _check("hwp_batch_fill", props) is None
    # 같은 이름 data 라도 다른 기능에서는 경로로 보지 않는다 (fill_fields 는 객체, set_chart_data 는 JSON 문자열)
    assert _check("hwp_set_chart_data", {"data": {"type": "string", "description": "편집 JSON (labels?, series…)"}}) is None


def test_server_blocked():
    for name, why in SERVER_BLOCKED.items():
        assert _check(name, {"path": {"type": "string"}}) == why
