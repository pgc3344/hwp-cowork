# rhwp 버전의 유일한 기준 — CLI(조사·편집)·studio(웹 에디터)·editor SDK 가 모두 이 값으로 만들어진다.
# 상류와 차이가 벌어지면 서버가 경고한다 (rhwp_update.py).
ARG RHWP_VERSION=v0.8.7

# ---- studio: rhwp-studio 를 이 서버 도메인(/studio/)에서 내려고 소스에서 빌드한다 (Rust 없이, wasm 은 npm @rhwp/core)
FROM node:22-bookworm AS studio
ARG RHWP_VERSION
WORKDIR /w
RUN git clone --depth 1 --branch "${RHWP_VERSION}" --filter=blob:none --sparse https://github.com/edwardkim/rhwp.git rhwp \
 && git -C rhwp sparse-checkout set rhwp-studio assets npm/hwpctrl-ocx \
 && git -C rhwp rev-parse HEAD > /w/commit
RUN mkdir -p rhwp/pkg && npm pack "@rhwp/core@${RHWP_VERSION#v}" --silent \
 && tar xzf rhwp-core-*.tgz -C rhwp/pkg --strip-components=1 \
 && npm pack "@rhwp/editor@${RHWP_VERSION#v}" --silent && mkdir editor && tar xzf rhwp-editor-*.tgz -C editor --strip-components=1
RUN cd rhwp/rhwp-studio && npm ci --no-audit --no-fund \
 && RHWP_DISABLE_EXTERNAL_WEBFONTS=1 npx vite build --base=/studio/
RUN mkdir -p /out/studio /out/editor \
 && cp -R rhwp/rhwp-studio/dist/. /out/studio/ \
 && (cp rhwp/pkg/LICENSE /out/studio/LICENSE || cp rhwp/LICENSE /out/studio/LICENSE) \
 && cp editor/index.js editor/transport.js editor/document-agent-contract.js /out/editor/ \
 && printf '@rhwp/editor %s (npm, MIT)\n' "${RHWP_VERSION#v}" > /out/editor/VERSION \
 && printf '{"engine":"rhwp","tag":"%s","commit":"%s","license":"MIT","copyright":"(c) 2025-2026 Edward Kim"}\n' \
      "${RHWP_VERSION}" "$(cat /w/commit)" > /out/studio/version.json

FROM python:3.13-slim
ARG RHWP_VERSION
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl librsvg2-bin fonts-nanum fonts-nanum-extra \
      fonts-noto-cjk fontconfig qpdf \
 && rm -rf /var/lib/apt/lists/*
# 한글 문서 글꼴 이름(함초롬바탕·맑은 고딕·HY헤드라인 등)을 있는 글꼴로 잇는다 — PDF·미리보기 공통
COPY docker/fonts-ko.conf /etc/fonts/conf.d/60-hwp-ko.conf
RUN fc-cache -f >/dev/null
RUN set -eux; arch="$(uname -m)"; case "$arch" in x86_64) a=x86_64;; aarch64) a=aarch64;; *) echo "unsupported $arch"; exit 1;; esac; \
    curl -fsSL -o /tmp/rhwp.tgz "https://github.com/edwardkim/rhwp/releases/download/${RHWP_VERSION}/rhwp-${RHWP_VERSION}-linux-${a}.tar.gz"; \
    curl -fsSL -o /tmp/SHA256SUMS.txt "https://github.com/edwardkim/rhwp/releases/download/${RHWP_VERSION}/SHA256SUMS.txt"; \
    cd /tmp && grep "linux-${a}.tar.gz" SHA256SUMS.txt | sed "s#  .*#  rhwp.tgz#" | sha256sum -c -; \
    tar xzf rhwp.tgz && install -m 0755 rhwp/rhwp /usr/local/bin/rhwp && rm -rf /tmp/rhwp* /tmp/SHA256SUMS.txt; \
    rhwp capabilities --mcp >/dev/null

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=studio /out/editor/ ./src/hwp_cowork/static/rhwp-editor/
RUN pip install --no-cache-dir .
COPY --from=studio /out/studio/ /app/studio/

ENV HWPCOWORK_TRANSPORT=http \
    HWPCOWORK_HOST=0.0.0.0 \
    HWPCOWORK_PORT=8119 \
    HWPCOWORK_RHWP_BIN=/usr/local/bin/rhwp \
    HWPCOWORK_RHWP_VERSION=${RHWP_VERSION} \
    HWPCOWORK_WORK_DIR=/work \
    HWPCOWORK_DATA_DIR=/data \
    HWPCOWORK_STUDIO_DIR=/app/studio \
    PYTHONUNBUFFERED=1

RUN useradd -m -u 10003 cowork && mkdir -p /work /data && chown cowork:cowork /work /data
USER cowork

EXPOSE 8119
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:8119/healthz || exit 1

CMD ["python", "-m", "hwp_cowork.server"]
