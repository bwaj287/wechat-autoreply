# V8 Second Ponytail Pass

Reviewed on 2026-10-17 using the installed Ponytail 5.3.2 skill in full mode. The expected load is one Mac, one WeChat account, and one launchd-managed runner.

## Scope

Reviewed V8's first refactor, shared JSON persistence and its callers, OCR panel extraction and its callers, queue/recheck policies, and related tests. The changes below remain on `feature/v8`; the V7 release, mainline, tag, and deployed V7 application code remain unchanged.

## Findings addressed

1. **Failed JSON writes left temporary files behind.** An unserializable payload or failed replacement kept the previous state intact but leaked a `.tmp` file on every failed attempt. The shared writer now closes the temporary handle, writes and replaces inside `try`, and removes the temporary path in `finally`. Regression tests cover serialization, writing, replacement, original-state preservation, and cleanup.
2. **Two OCR extractors duplicated the same message-building logic.** Main-window and standalone-window parsing differed only in spatial filtering. The standalone entry point now reuses the shared extractor, retaining its own top/width thresholds and bottom-toolbar exclusion. Sender classification, metadata, noise filtering, and line grouping remain unchanged.

No new dependencies, user-facing options, state formats, model settings, or sending policies were added. Production code decreased by 46 lines in this pass, or 92 lines compared with the V7 tag. Tests and documentation are additional and are not included in that reduction.

## Validation

- 50 unit tests passed.
- The complete fake-UI `selftest.py` passed.
- All 13 geometry-boundary cases passed against the old extractors before the refactor and the new extractors afterward.
- All 400 comparisons with the previous extractors matched, using deterministic synthetic panels with sender roles, color evidence, media, titles, timestamps, and noise.
- `git diff --check` passed.

Not checked: a full repository security audit, every real WeChat layout, or delivery of a newly sent real message. Retain V7 as the rollback reference during live use.
