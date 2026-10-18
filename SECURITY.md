# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x (>= 0.1.1) | :white_check_mark: |
| 0.1.0   | :x:                |

Please always use the latest release.

## Reporting a Vulnerability

Please **do not** report security vulnerabilities through public GitHub issues.

Instead, report them privately via GitHub's
[private vulnerability reporting](https://github.com/pgc3344/hwp-cowork/security/advisories/new).

Please include:
- Affected version and configuration (e.g. auth mode, `WRITABLE_ROOTS`)
- Steps to reproduce
- Impact

I will acknowledge your report within 7 days and keep you updated on the fix.
Confirmed issues will be fixed in a new release and disclosed through a
GitHub Security Advisory, with credit to the reporter unless you prefer otherwise.

## Past Advisories

- [GHSA-2wm8-m6g7-r39c](https://github.com/pgc3344/hwp-cowork/security/advisories/GHSA-2wm8-m6g7-r39c) — Missing authorization check on save target root (fixed in 0.1.1)
