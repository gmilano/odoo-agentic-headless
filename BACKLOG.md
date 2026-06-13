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

## P0 — ERP Comprehension Core

- [ ] `AH-0005` Add persistent `agentic.request.log` model for every API call.
- [ ] `AH-0006` Add `business_snapshot` trend memory: compare current counts with previous snapshot.
- [ ] `AH-0007` Add `/agentic/v1/capabilities`: models, installed domains, allowed operations, risky operations.
- [ ] `AH-0008` Add `/agentic/v1/business_events`: normalized recent changes across CRM, Sales, Inventory, Accounting, Projects.
- [ ] `AH-0009` Add `/agentic/v1/action_plan`: read a natural language goal, return typed executable Odoo operations without executing.
- [ ] `AH-0010` Add `/agentic/v1/execute_plan`: execute approved action plans with audit log and rollback hints.

## P1 — SAP-Replacement Demo Surface

- [ ] `AH-0101` Install and seed CRM/Sales/Inventory/Accounting demo modules for a believable mid-market company.
- [ ] `AH-0102` Build executive "business cockpit" JSON: revenue, pipeline, cash, inventory risk, delivery risk.
- [ ] `AH-0103` Add Claude/Wany tool adapter that maps model tool calls to the headless API.
- [ ] `AH-0104` Add permission profiles: executive read-only, ops operator, finance operator, admin.
- [ ] `AH-0105` Add risk classifier for destructive or financial operations.
- [ ] `AH-0106` Add approval queue inside Odoo for risky agent actions.

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

