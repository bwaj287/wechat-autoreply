# V7 Stable Baseline

Freeze the V7 code running on this Mac on 2026-10-17 as the comparison and rollback baseline for V8 simplification.

- Previously deployed code: `1d2f044` (`fix: keep runtime control files project-relative`).
- Stable development branch: `feature/v7_iteration`; release branch: `release`; stable mainline: `main`.
- Stable tag: `v7-stable-20261017`; next development branch: `feature/v8`.
- Preserved V7 application: `/Users/shawnwang/.openclaw/runtime/wechat-autoreply-v7-app`.
- Service label remains `ai.openclaw.wechat.autoreply.v6`; this historical name does not indicate the code version.
- Baseline checks: 43 unit tests and the complete `selftest.py` passed.

V7 resolves the allowlist and switch relative to the application root. Keep that fix in later deployments. Cleanup reports, maintenance scripts, local environments, conversation data, contact records, switches, and allowlists are excluded from this release commit.

## V8 validation and rollback

1. Create `feature/v8` from the stable tag. Start with changes that preserve behavior.
2. Run all unit tests and the fake-UI selftest before preparing a separate V8 application directory.
3. Use the existing single launchd job for the Mac trial. Never run V7 and V8 senders simultaneously.
4. Preserve the current switch, allowlist, configuration, contact memory, and shared state. Do not reset pending items or recent send history.
5. On failure, point the same service back to the preserved V7 application and compare V8 with the stable tag.

V7 and V8 retain the same state format. Roll back code while keeping the latest state; restoring an older queue snapshot can repeat already processed sends. Recognition and delivery with real messages still require a live trial on this Mac.
