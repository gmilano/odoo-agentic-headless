# Odoo Agentic Headless Backlog

Vision: make Odoo usable as a headless, agent-native ERP layer that can replace
SAP-style operational workflows through a business comprehension interface.

The product should not be "chat over ERP". It should expose a typed, auditable
understanding layer: what the business is, what is changing, what needs action,
and which agent can safely execute it.

## Now

- [x] `AH-0001` Bootstrap Odoo 19.0 source + custom addon workspace.
- [x] `AH-0002` Add secured headless CRUD/schema/call API.
- [x] `AH-0003` Add Docker runtime with Postgres and Odoo 19.0.
- [x] `AH-0004` Add first business comprehension endpoint: `business_snapshot`.
- [x] `AH-0007` Add `/agentic/v1/capabilities`: models, installed domains, allowed operations, risky operations.
- [x] `AH-0006` Add `business_snapshot` trend memory: compare current counts with previous snapshot.
- [x] `AH-0008` Add `/agentic/v1/business_events`: normalized recent changes across CRM, Sales, Inventory, Accounting, Projects.
- [x] `AH-0009` Add `/agentic/v1/action_plan`: read a natural language goal, return typed executable Odoo operations without executing.
- [x] `AH-0012` Add `/agentic/v1/okf_bundle`: export Odoo business context as Open Knowledge Format v0.1 Markdown files.

## P0 — ERP Comprehension Core

- [x] `AH-0005` Add persistent `agentic.request.log` model for every API call.
- [ ] `AH-0010` Add `/agentic/v1/execute_plan`: execute approved action plans with audit log and rollback hints.
- [x] `AH-0011` Add audit-log query filters and retention policy for agent reviews.

## P1 — SAP-Replacement Demo Surface

- [ ] `AH-0101` Install and seed CRM/Sales/Inventory/Accounting demo modules for a believable mid-market company.
- [ ] `AH-0102` Build executive "business cockpit" JSON: revenue, pipeline, cash, inventory risk, delivery risk.
- [ ] `AH-0103` Add Claude/Wany tool adapter that maps model tool calls to the headless API.
- [ ] `AH-0104` Add permission profiles: executive read-only, ops operator, finance operator, admin.
- [ ] `AH-0105` Add risk classifier for destructive or financial operations.
- [ ] `AH-0106` Add approval queue inside Odoo for risky agent actions.
- [ ] `AH-0107` Materialize OKF bundles to disk/git and add a static graph viewer for business concepts.

## P2 — Market Story

- [ ] `AH-0201` Write the manifesto: "The Next ERP Has No Screens First".
- [ ] `AH-0202` Build demo script: "Replace SAP workflow from Claude in 7 minutes".
- [ ] `AH-0203` Add screencast-ready seed data with sales, purchase, stock and invoices.
- [ ] `AH-0204` Create benchmark: time-to-answer and time-to-action vs traditional ERP navigation.
- [ ] `AH-0205` Publish architecture diagram: Odoo core + agentic API + Claude/Wany + audit/approval layer.

## Daily Loop Rule

Every day at 08:00 America/Montevideo:

1. Read this backlog and `docs/DAILY_LOOP.md`.
2. Pick one small P0/P1 item that can be completed or advanced in one focused pass.
3. Implement it in `custom_addons/agentic_headless` or supporting docs/scripts.
4. Run the fastest relevant verification.
5. Commit the work locally.
6. Update this backlog with status, new follow-up ideas, and today's notes.
7. Send Gastón a concise Telegram summary with what changed, tests, and next target.

## Daily Notes

- 2026-06-13 08:03 America/Montevideo: completed `AH-0007` with authenticated `GET|POST /agentic/v1/capabilities`. It reports addon/API version, installed modules, tracked ERP model availability/fields, allowed operations, risky operations, guardrail flags, and the next safety gaps. Follow-up: implement `AH-0005` so these calls become auditable instead of only discoverable.
- 2026-06-14 08:05 America/Montevideo: completed `AH-0005` with persistent `agentic.request.log` audit rows for API responses, including endpoint, operation, model, status, auth result, error code, payload, and response snapshots. Verified module upgrade, capabilities smoke, and unauthorized request logging. Follow-up: use the audit trail as the substrate for `AH-0009`/`AH-0010` approval-safe plan execution.
- 2026-06-15 08:01 America/Montevideo: completed `AH-0006` by adding `trend_memory` to `/agentic/v1/business_snapshot`, comparing current tracked ERP model counts with the previous successful audited snapshot. Also corrected capabilities safety gaps now that request logging exists. Follow-up: implement `AH-0008` business events from recent `create_date`/`write_date` activity.
- 2026-06-16 08:01 America/Montevideo: completed `AH-0008` with authenticated `GET|POST /agentic/v1/business_events`. It normalizes recent CRM, Sales, Inventory, Accounting, and Project record activity from `create_date`/`write_date`, returns domain coverage, event summaries, actor metadata, signals, and quiet-domain insights. Verified syntax, health, and endpoint smoke against the running Docker Odoo. Follow-up: implement `AH-0009` action-plan generation using capabilities, business snapshots, and business events as planning context.
- 2026-06-17 08:02 America/Montevideo: completed `AH-0009` with authenticated `POST /agentic/v1/action_plan`. It maps common natural-language ERP goals into typed non-executing operation plans, exposes risk/approval metadata, blocks plans when required Odoo models are unavailable, and documents the future `execute_plan` contract. Verified syntax, health, and endpoint smoke against Docker Odoo. Follow-up: implement `AH-0010` to execute reviewed plans with audit log links and rollback hints.
- 2026-06-17 10:55 America/Montevideo: completed `AH-0012` after Google Cloud introduced Open Knowledge Format (OKF). Added authenticated `GET|POST /agentic/v1/okf_bundle`, producing OKF v0.1 Markdown file entries for company context, capabilities, ERP domains, operations, and update log. Follow-up: materialize bundles to git and build a viewer/indexer so the ERP becomes an agent-readable business wiki.
- 2026-06-18 08:03 America/Montevideo: completed `AH-0011` with authenticated `GET|POST /agentic/v1/audit_logs`. It filters `agentic.request.log` by endpoint, operation, model, status, auth result, error code, and recent window, optionally includes captured payload/response JSON, and reports a 90-day retention policy with expired-log counts. Follow-up: implement `AH-0010` on top of this audit trail so approved execution can link every operation to reviewable logs and rollback hints.
