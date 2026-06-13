# Daily Odoo Agentic Loop

This is the operating protocol for the daily 08:00 job.

## Goal

Advance Odoo Agentic Headless every day toward an agent-native ERP that can act
as a credible SAP alternative through Claude/Wany.

## Constraints

- Keep changes small enough to verify in the same run.
- Prefer real Odoo addon code over speculative docs.
- Do not edit upstream `vendor/odoo` unless the task explicitly requires it.
- Commit every completed change.
- If no Git remote exists, say so; do not invent one.
- Keep Odoo running with `scripts/dev-compose` when verification needs HTTP.

## Runbook

```bash
cd ~/Projects/odoo-agentic-headless
scripts/dev-compose ps
curl -s http://localhost:8069/agentic/v1/health
```

Pick the next backlog item using this priority:

1. P0 items that deepen business comprehension or agent safety.
2. P1 items that improve the SAP-replacement demo.
3. P2 market narrative only if code is already healthy.

Verification checklist:

- `python3 -m py_compile custom_addons/agentic_headless/controllers/main.py`
- HTTP smoke test for any changed endpoint.
- `git status --short`
- `git commit -m "..."`

Telegram summary format:

```text
Odoo loop 08:00:
- Implemented: ...
- Verified: ...
- Commit: ...
- Next: ...
```

