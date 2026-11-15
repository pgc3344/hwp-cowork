# Third-party notices

hwp-cowork는 한글 문서 엔진으로 **rhwp**를 사용합니다. rhwp는 hwp-cowork와 별개 프로젝트이며, hwp-cowork는 rhwp 작성자가 만들거나 보증한 것이 아닙니다.

- **rhwp** (CLI, rhwp-studio, `@rhwp/core`, `@rhwp/editor`) — https://github.com/edwardkim/rhwp
  MIT License, Copyright (c) 2025-2026 Edward Kim
  - `src/hwp_cowork/static/rhwp-editor/`는 npm `@rhwp/editor`에서 가져온 파일입니다.
  - Docker 이미지는 rhwp 릴리스 바이너리(SHA256 검증)와 rhwp-studio 빌드 결과물을 포함하며, 이미지 안 `/app/studio/LICENSE`에 원본 라이선스가 들어 있습니다.
  - rhwp 바이너리·studio 에 포함된 서드파티 구성요소(Rust 라이브러리 약 400개 — MIT·Apache-2.0·BSD·Zlib·ISC 등 허용형 라이선스, PDF 가져오기의 PDFOxide·OfficeOxide(MIT) 포함, 웹 글꼴(SIL OFL 1.1·GUST), 포팅한 알고리즘(Volexity hwp-extract, BSD-3-Clause))의 목록과 고지는 이 저장소가 쓰는 rhwp 판의 [THIRD_PARTY_LICENSES.md](https://github.com/edwardkim/rhwp/blob/v0.9.0/THIRD_PARTY_LICENSES.md)에 있습니다 (판은 `Dockerfile`의 `RHWP_VERSION`).

"한글", "HWP"는 한글과컴퓨터의 상표입니다. "Claude"는 Anthropic의 상표입니다. hwp-cowork는 두 회사와 관계가 없습니다.
