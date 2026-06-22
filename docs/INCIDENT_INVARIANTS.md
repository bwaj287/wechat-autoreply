# Incident-Derived Invariants

These rules come from failures observed during V3-V5 and are release gates for V6.

| Incident class | Required invariant | Primary coverage |
| --- | --- | --- |
| Dock signal missing | Background roster preflight can recover a whitelist numeric badge | `test_claim_policy.py`, `selftest.py` |
| No unread but WeChat opens repeatedly | Passive preflight without a badge never enters foreground claim flow | `test_claim_policy.py`, `selftest.py` |
| Warm/red avatar looks unread | Red pixels alone are not actionable; digit evidence is mandatory | `test_claim_policy.py`, synthetic badge selftest |
| Contact replies with text similar to our outbound | A confirmed latest gray inbound bubble outranks text self-echo similarity | `test_outbound_history.py`, Darren regression in `selftest.py` |
| Stale OCR frame changes message direction | Send-time decisions use multi-frame consensus | `test_recheck_policy.py` |
| User already replied manually | New right-aligned outbound evidence cancels only after reliability checks | `test_manual_reply_policy.py`, manual reply selftests |
| Send occurred but verification missed it | Recent outbound/self-echo history can confirm late sends without duplicate resend | `test_outbound_history.py`, send confirmation selftests |
| Native tool appends diagnostics/duplicate JSON | Parse the first complete JSON object | JSON output selftest |
| Queue contains multiple messages for one contact | Remove by inbound fingerprint, never by contact | `test_pending_queue.py` |
| Event write and retention cleanup overlap | Both operations share one lock, retry transient macOS errors, and preserve valid JSONL | concurrent path in `test_event_log.py` |
| Event file temporarily unavailable | Retry and fallback logging; never crash the runner | fallback path in `test_event_log.py` |
| Cleanup fails while idle | Record `runtime_cleanup_failed` and continue the tick | orchestrator integration path |
