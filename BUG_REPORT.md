# Historical Bug Report — Agent Workspace v0.4.3

This document records ten findings reported during the v0.4.3 review and the fixes claimed at that time. It is retained as historical context, not as a current bug inventory or current test report. The present codebase has changed since that review; consult `AUDIT_FINAL.md` for issues confirmed against current source.

## Findings reported fixed in v0.4.3

| ID | Area | Finding at the time | Reported status |
|---|---|---|---|
| BUG-01 | `app/agents/runtime.py` | Invalid/empty provider responses were not retried | Fixed in v0.4.3 |
| BUG-02 | `app/db.py` | `save_message` did not upsert message IDs | Fixed in v0.4.3 |
| BUG-03 | `app/agents/orchestrator.py` | Partial streaming messages were not persisted immediately | Fixed in v0.4.3 |
| BUG-04 | `app/web/static/app.js` | Polling did not stop promptly on terminal WebSocket update | Fixed in v0.4.3 |
| BUG-05 | `app/web/static/app.js` | Custom worker avatar styling used hard-coded agent IDs | Fixed in v0.4.3 |
| BUG-06 | `app/web/static/app.js` | Agent data could remain stale after changes | Fixed in v0.4.3 |
| BUG-07 | `app/config.py` | Relative DB paths depended on launch directory | Fixed in v0.4.3 |
| BUG-08 | `app/bus.py` | Subscriber queues were unbounded | Fixed in v0.4.3 |
| BUG-09 | `app/web/static/app.js` | GET requests always sent JSON Content-Type | Fixed in v0.4.3 |
| BUG-10 | `app/agents/orchestrator.py` | Persistence failure in error handling could mask original exception | Fixed in v0.4.3 |

## Validation recorded by the original report

The report recorded `pytest tests -q` as 19/19 passing, Python compilation and JavaScript syntax checks as passing, and a set of browser checks. These are historical claims dated 2026-11-24 and have not been rerun as part of the documentation reconciliation dated 2026-11-25.

For current code review findings and follow-up priorities, see `AUDIT_FINAL.md`. For implemented behavior and operational limitations, see `README.md`.
