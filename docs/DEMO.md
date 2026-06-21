# Agentic ERP Demo

Goal: show Odoo as an agent-operated ERP with human approval, auditability, and rollback.

## Setup

```bash
cd ~/Projects/odoo-agentic-headless
DOCKER_API_VERSION=1.44 /opt/homebrew/bin/docker compose up -d
scripts/seed-demo-company
DOCKER_API_VERSION=1.44 /opt/homebrew/bin/docker compose restart odoo
```

Open:

- Mobile approval UI: http://localhost:8069/agentic/ui/approvals
- Odoo backend: http://localhost:8069/web
- Login: `admin` / `admin`

## Seven-Minute Story

1. Open `http://localhost:8069/agentic/ui/approvals` on mobile.
2. Tap **Create demo approval**.
3. Show that the generated request is an `AHR-...` approval for a CRM opportunity escalation.
4. Tap **Approve**.
5. Tap **Execute approved plan**.
6. Open Odoo backend, then **Agentic ERP > Approvals**.
7. Show the request is now `consumed`.
8. Open **Agentic ERP > Audit Logs**.
9. Show the executed operation and rollback hints.

## API Proof Points

Write without durable approval is blocked:

```bash
curl -sS http://localhost:8069/agentic/v1/execute_plan \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"approved":true,"plan":{"operations":[{"operation":"write","payload":{"model":"res.partner","ids":[1],"values":{"phone":"+598 123"}}}]}}'
```

Expected error:

```json
{
  "ok": false,
  "error": {
    "code": "durable_approval_required"
  }
}
```

Business surface:

```bash
curl -sS http://localhost:8069/agentic/v1/business_snapshot \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"sample_limit":2}'
```

## Talk Track

This is not a chatbot over ERP. The agent can read and plan freely, but risky writes become durable approval requests inside Odoo. Operators approve from Odoo or mobile, execution consumes the approval, and every write returns concrete rollback data.
