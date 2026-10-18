# hwp-cowork

**Claude와 함께 쓰는 한글(HWP) 동시편집 서버.** Claude가 문서를 고치는 동안 사람도 같은 문서를 브라우저에서 보고 직접 고칩니다. 바뀐 내용은 모두 판(v1, v2 …)으로 남습니다.

*Co-edit Korean HWP/HWPX documents with Claude (or any MCP client). Self-hosted with Docker, no Hancom Office required.*

- **한컴오피스 없이** Linux 서버에서 `.hwp`와 `.hwpx`를 조사하고 편집합니다. 엔진은 [rhwp](https://github.com/edwardkim/rhwp)입니다.
- **원본은 손대지 않습니다.** 편집하면 서버가 사본을 만들어 판(v0 → v1 → v2 …)을 쌓고, 비교와 되돌리기를 지원합니다.
- **사람과 AI가 같이 고칩니다.** 실시간 화면에서 Claude의 편집이 1초 안에 보이고, 사람이 고친 내용은 다음 호출 때 Claude에게 전달됩니다(`human_edits`). 동시에 고칠 때 생기는 충돌도 처리합니다.
- **채팅 안 미리보기.** MCP Apps를 지원하는 클라이언트(claude.ai, Claude 데스크톱 등)에서는 도구 결과 아래에 쪽 미리보기가 뜹니다.
- **원하는 인증을 고릅니다.** 인증 없음, Bearer 토큰, OIDC(Authentik·Keycloak·Zitadel …) 중에서 선택합니다.
- **`docker compose up` 한 번으로 설치합니다.**

> 혼자 로컬에서 Claude Code로만 쓴다면 rhwp에 내장된 MCP(`rhwp mcp-serve`)로도 충분합니다.
> hwp-cowork는 그 위에 **판 관리, 실시간 공동 편집 화면, 원격 다중 사용자 서버**를 얹은 것입니다.

## 빠른 시작

```bash
git clone https://github.com/pgc3344/hwp-cowork && cd hwp-cowork
mkdir -p docs && cp ~/내문서.hwp docs/
docker compose up -d --build
```

이제 MCP 주소는 `http://localhost:8119/mcp`입니다. 기본값은 **인증 없음**이고 **127.0.0.1에서만** 열립니다.

### 클라이언트 연결

**Claude Code**

```bash
claude mcp add --transport http hwp http://localhost:8119/mcp
```

**Claude 데스크톱이나 다른 MCP 클라이언트**: streamable HTTP 주소 `http://localhost:8119/mcp`를 등록합니다. token 모드라면 `Authorization: Bearer <토큰>` 헤더를 붙입니다.

**claude.ai 커스텀 커넥터**: 외부에서 닿는 HTTPS 주소가 필요합니다. 리버스 프록시나 터널 뒤에 두고, 인증은 `oidc` 모드를 씁니다(아래 참고).

연결한 뒤에는 이렇게 말하면 됩니다. "`docs/내문서.hwp`에서 표 3번째 행 금액을 고치고 실시간으로 보여줘."

## 설정

`cp .env.example .env`로 복사한 뒤 고칩니다. 주요 항목은 다음과 같습니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `HWPCOWORK_ROOTS` | `docs=/docs` | `이름=경로` 목록(쉼표 구분). 문서 경로는 `이름/하위/파일.hwp` 꼴입니다 |
| `HWPCOWORK_WRITABLE_ROOTS` | (비움) | 사본을 원본 폴더에 직접 저장해도 되는 루트. 비우면 읽기 전용이고 내려받기로만 꺼냅니다 |
| `HWPCOWORK_PUBLIC_BASE` | `http://localhost:8119` | 링크와 OAuth에 쓰는 바깥 주소 |
| `HWPCOWORK_BIND` | `127.0.0.1` | 포트를 열 주소. `0.0.0.0`으로 열 때는 인증을 꼭 켜세요 |
| `HWPCOWORK_AUTH` | `none` | `none` · `token` · `oidc` |
| `HWPCOWORK_KEEP_DAYS` | `30` | 손대지 않은 작업 사본을 보관하는 일수 |
| `HWPCOWORK_ALLOWED_HOSTS` | (비움) | 리버스 프록시 호스트. DNS rebinding을 막습니다 |

### 인증

| 모드 | 쓰는 곳 | 설정 |
|---|---|---|
| `none` | 내 컴퓨터, 사내망, 인증 프록시(Cloudflare Access·oauth2-proxy 등) 뒤 | 없음 |
| `token` | Claude Code·데스크톱 등 헤더를 붙일 수 있는 클라이언트 | `HWPCOWORK_TOKEN=$(openssl rand -hex 32)`. 여러 사람이면 `HWPCOWORK_TOKENS_FILE`에 `[{"token","label","roots","write"}]` |
| `oidc` | claude.ai 커넥터(OAuth 로그인), 팀 | `HWPCOWORK_OIDC_ISSUER`, `HWPCOWORK_OIDC_CLIENT_ID`, `HWPCOWORK_RESOURCE_URL`, `HWPCOWORK_PRINCIPALS_FILE` |

`oidc` 모드에서 지켜야 할 것:
- 제공자 쪽 클라이언트는 **public(PKCE)**이어야 합니다.
- redirect URI에 `<PUBLIC_BASE>/sso/callback`을 넣습니다.
- 서버가 동적 클라이언트 등록(DCR)을 대신 처리하므로, DCR이 없는 제공자도 claude.ai 커넥터에 주소만 넣으면 됩니다.
- 사람별 권한은 [`principals.example.json`](principals.example.json) 형식으로 둡니다. 실시간 화면 링크는 링크 주인이 로그인한 브라우저에서만 열립니다.

`none`과 `token` 모드의 실시간 화면 링크는 링크 코드(12시간 유효)만으로 열립니다.

### 원본 폴더에 사본 저장

```yaml
# docker-compose.yml
environment:
  HWPCOWORK_ROOTS: docs=/docs
  HWPCOWORK_WRITABLE_ROOTS: docs
volumes:
  - ./docs:/docs        # :ro 를 뗍니다
```

컨테이너는 uid `10003`으로 돌기 때문에 `sudo chown -R 10003 docs`처럼 쓰기 권한을 줘야 합니다. 저장하면 원본 옆에 `이름_vN.hwp`가 생기고, 원본은 절대 덮어쓰지 않습니다.

## 도구

| 묶음 | 도구 |
|---|---|
| 조사 | `hwp_info` `hwp_locate` `hwp_tables` `hwp_fields` `hwp_structure` `hwp_extract` `hwp_text` |
| 편집(새 판) | `hwp_fill_fields` `hwp_set_cell` `hwp_replace_text` |
| 새 문서 | `hwp_templates` → `hwp_new` |
| 확인 | `hwp_preview` `hwp_diff` `hwp_undo` `hwp_view_link`(실시간 화면) `hwp_jobs` |
| 반출 | `hwp_save_copy` `hwp_download_link` |
| 올리기 | `hwp_upload_link`(채팅 첨부 → 새 작업) |
| 전체 기능 | `hwp_catalog` → `hwp_run`: 표 병합, 각주, 머리말, 쪽 설정, 그림, 수식, 차트, 개인정보 가리기, PDF 등 rhwp 기능 전부 |

`hwp_run`의 기능 목록은 rhwp가 직접 내보냅니다(`rhwp capabilities --mcp`). 그래서 rhwp 버전을 올리면 코드를 고치지 않아도 기능이 늘어납니다.

## 실시간 화면

`hwp_view_link`나 편집 결과로 받은 링크를 열면 다음을 할 수 있습니다.

- 판 기록: 누가(Claude·사람·원본) 무엇을 바꿨는지, 판마다 바뀐 곳, 옛 판 보기와 되돌리기
- 직접 편집: 손을 멈추고 4초가 지나면 자동으로 새 판이 됩니다. Claude가 고치는 중이면 화면에 표시되고, 충돌이 나면 어느 쪽을 남길지 고릅니다
- 다운로드, 그리고 원본 폴더에 사본 저장(쓰기 허용 루트일 때)

## 보안 설계

- 원본 루트는 읽기 전용으로 마운트하는 것을 권장합니다. 편집은 `/work` 샌드박스 안에서만 일어납니다.
- rhwp에 넘기는 경로 인자는 모두 서버가 바꿔 넣습니다. 분류되지 않은 경로 인자는 막고(fail-closed), 응답에 실제 경로가 나가지 않습니다.
- 짧은 링크 코드를 무작위로 대입하면 IP마다 막습니다(10분에 20회).
- 컨테이너는 `read_only`, `cap_drop: ALL`, `no-new-privileges`, 비root 사용자로 돕니다.

## 개발

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest -q tests --ignore=tests/test_e2e.py
PYTHONPATH=src .venv/bin/python tests/test_apps.py
# rhwp 바이너리와 표가 있는 샘플 문서로 종단 테스트
HWPCOWORK_RHWP_BIN=/path/to/rhwp HWPCOWORK_TEST_HWP=sample.hwp .venv/bin/python tests/test_e2e.py
```

rhwp 버전은 `Dockerfile`의 `RHWP_VERSION` 한 곳에서 정합니다. 그 값 하나로 CLI, studio, editor가 함께 맞춰집니다.

## 라이선스

MIT. rhwp(MIT, © Edward Kim)를 사용하며, 자세한 내용은 [NOTICE.md](NOTICE.md)에 있습니다.
"한글"과 "HWP"는 한글과컴퓨터의 상표이고, "Claude"는 Anthropic의 상표입니다. 이 프로젝트는 두 회사와 관계가 없습니다.
