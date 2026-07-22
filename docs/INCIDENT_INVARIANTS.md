# Incident-Derived Invariants

These rules come from failures observed during V3-V5 and are release gates for V6.

| Incident class | Required invariant | Primary coverage |
| --- | --- | --- |
| Dock signal missing | Background roster preflight can recover a whitelist numeric badge | `test_claim_policy.py`, `selftest.py` |
| Runner is alive while WeChat is logged out | Login entry UI is reported as `wechat_login_required`, never as an empty healthy roster | login detection selftest |
| Global unread belongs to non-whitelist contact | If the left roster has no whitelist numeric badge, clear non-whitelist badges and do not search whitelist contacts | `selftest.py` |
| No unread but WeChat opens repeatedly | Passive preflight without a badge never enters foreground claim flow | `test_claim_policy.py`, `selftest.py` |
| Warm/red avatar looks unread | Red pixels alone are not actionable; verified digit evidence is mandatory, and a warm avatar cannot dilute a real digit badge | `test_claim_policy.py`, synthetic badge selftest |
| Clicking an unread row clears its badge before title OCR confirms | Matching clicked contact, gray inbound panel, and roster preview can confirm claim and send-time selection; otherwise retry with a finite budget | claim and pending selection selftests |
| Contact replies with text similar to our outbound | A confirmed latest gray inbound bubble outranks text self-echo similarity | `test_outbound_history.py`, Darren regression in `selftest.py` |
| Stale OCR frame changes message direction | Send-time decisions use multi-frame consensus | `test_recheck_policy.py` |
| User already replied manually | New right-aligned outbound evidence cancels only after reliability checks | `test_manual_reply_policy.py`, manual reply selftests |
| Send occurred but verification missed it | Recent outbound/self-echo history can confirm late sends; confirmation retries are bounded and time out as inferred sent without duplicate resend | `test_outbound_history.py`, send confirmation selftests |
| Native tool appends diagnostics/duplicate JSON | Parse the first complete JSON object | JSON output selftest |
| Queue contains multiple messages for one contact | Remove by inbound fingerprint, never by contact | `test_pending_queue.py` |
| Event write and retention cleanup overlap | Both operations share one lock, retry transient macOS errors, and preserve valid JSONL | concurrent path in `test_event_log.py` |
| Event file temporarily unavailable | Retry and fallback logging; never crash the runner | fallback path in `test_event_log.py` |
| Cleanup fails while idle | Record `runtime_cleanup_failed` and continue the tick | orchestrator integration path |
