# Odoo Agentic Headless

Headless, agent-friendly extension layer for Odoo.

This workspace keeps upstream Odoo in `vendor/odoo` and custom modules in
`custom_addons`. The first module is `agentic_headless`, a small JSON API for
agents to inspect and operate on Odoo models without using the web UI.

## Layout

```text
vendor/odoo/                 Upstream Odoo 19.0 shallow clone
custom_addons/agentic_headless/
config/odoo.conf.example     Local config template
scripts/dev-odoo             Local runner
```

## First Run

Install Odoo's system dependencies for macOS first. PostgreSQL must be running
and a database user must exist.

```bash
cd ~/Projects/odoo-agentic-headless
python3 -m venv .venv
. .venv/bin/activate
pip install -r vendor/odoo/requirements.txt
cp config/odoo.conf.example config/odoo.conf
scripts/dev-odoo --init agentic_headless --database odoo_agentic
```

## Agentic API

Set `AGENTIC_HEADLESS_API_KEY` before running Odoo. API calls must send:

```text
Authorization: Bearer <key>
```

Endpoints:

- `GET /agentic/v1/health`
- `POST /agentic/v1/schema`
- `POST /agentic/v1/search_read`
- `POST /agentic/v1/create`
- `POST /agentic/v1/write`
- `POST /agentic/v1/call`

Example:

```bash
curl -s http://localhost:8069/agentic/v1/search_read \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"model":"res.partner","domain":[],"fields":["name","email"],"limit":5}'
```

