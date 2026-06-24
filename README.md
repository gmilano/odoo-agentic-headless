<div align="center">

# 🧠 Odoo Agentic Headless

**A headless, agent-friendly control plane for Odoo.**

Let AI agents *inspect*, *understand*, *plan* and *safely operate* on Odoo —
without ever touching the web UI.

[![Odoo](https://img.shields.io/badge/Odoo-19.0-714B67?logo=odoo&logoColor=white)](https://www.odoo.com/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![API](https://img.shields.io/badge/API-v1-00B894)](#-agentic-api)
[![License](https://img.shields.io/badge/License-LGPL--3.0-blue.svg)](LICENSE)

</div>

---

## ✨ What is this?

This workspace keeps **upstream Odoo** in `vendor/odoo` and your **custom modules**
in `custom_addons`. The flagship module is **`agentic_headless`** — a compact,
guard-railed JSON API that gives agents everything they need to work with Odoo
programmatically:

| 🎯 Capability | What it gives an agent |
|---|---|
| 🔍 **Inspect** | Schemas, records, installed modules & capabilities |
| 📊 **Understand** | Business snapshots, normalized events, an executive cockpit |
| 🧭 **Plan** | Safe action plans that never execute on their own |
| ✅ **Approve** | A durable approval queue + a mobile approval UI |
| ⚙️ **Operate** | Create / write / call — gated behind auth, risk tiers & audit logs |
| 📚 **Export** | Open Knowledge Format (OKF) bundles for an agent-readable wiki |

---

## 🗂️ Layout

```text
vendor/odoo/                          Upstream Odoo 19.0 shallow clone
custom_addons/agentic_headless/       The agentic JSON API module
config/odoo.conf.example              Local config template
scripts/dev-odoo                      Local source runner
scripts/dev-compose                   Docker runner
scripts/seed-demo-company             Full demo company seeder
docs/                                 DEMO.md · DAILY_LOOP.md
BACKLOG.md                            Working product backlog
```

---

## 🚀 First Run

### ⚡ Fast path — Docker

```bash
cd ~/Projects/odoo-agentic-headless
scripts/dev-compose up
```

Smoke test it:

```bash
curl -s http://localhost:8069/agentic/v1/health

curl -s http://localhost:8069/agentic/v1/search_read \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"model":"res.partner","domain":[],"fields":["name","email"],"limit":5}'
```

### 🛠️ Source path — deeper Odoo development

Install Odoo's macOS system dependencies first. PostgreSQL must be running and a
database user must exist.

```bash
cd ~/Projects/odoo-agentic-headless
$(brew --prefix python@3.12)/bin/python3.12 -m venv .venv
. .venv/bin/activate
pip install -r vendor/odoo/requirements.txt
cp config/odoo.conf.example config/odoo.conf
scripts/dev-odoo --init agentic_headless --database odoo_agentic
```

---

## 🔌 Agentic API

> 🔐 **Auth** — Set `AGENTIC_HEADLESS_API_KEY` before running Odoo.
> Every call must send: `Authorization: Bearer <key>`

Operations are tiered by risk. High-risk ones are gated behind explicit approval.

| Endpoint | Method | 🚦 Risk | Effect |
|---|---|:--:|---|
| `/agentic/v1/health` | `GET` | 🟢 | Liveness check |
| `/agentic/v1/capabilities` | `GET·POST` | 🟢 | Modules, models & guardrails self-description |
| `/agentic/v1/schema` | `POST` | 🟢 | Read model metadata |
| `/agentic/v1/search_read` | `POST` | 🟢 | Read records |
| `/agentic/v1/business_snapshot` | `GET·POST` | 🟢 | Business comprehension summary |
| `/agentic/v1/business_events` | `GET·POST` | 🟢 | Normalized recent changes |
| `/agentic/v1/business_cockpit` | `GET·POST` | 🟢 | Executive metrics + insights |
| `/agentic/v1/okf_bundle` | `GET·POST` | 🟢 | Export OKF Markdown bundle |
| `/agentic/v1/audit_logs` | `GET·POST` | 🟢 | Review the API audit trail |
| `/agentic/v1/action_plan` | `POST` | 🟢 | Plan operations — **does not execute** |
| `/agentic/v1/approval_requests` | `GET·POST` | 🟡 | Create / list durable approvals |
| `/agentic/v1/create` | `POST` | 🟡 | Create a record |
| `/agentic/v1/write` | `POST` | 🟡 | Update records |
| `/agentic/v1/execute_plan` | `POST` | 🔴 | Execute an **approved** plan |
| `/agentic/v1/call` | `POST` | 🔴 | Invoke a model method |

🛡️ **Guardrails:** Bearer-token auth · private methods blocked ·
`search_read` capped at 200 · 90-day audit log · approval-gated execution.
Ask the API to describe itself anytime via `/agentic/v1/capabilities`.

### 🧪 Examples

**Read records**

```bash
curl -s http://localhost:8069/agentic/v1/search_read \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"model":"res.partner","domain":[],"fields":["name","email"],"limit":5}'
```

**Business comprehension snapshot**

```bash
curl -s http://localhost:8069/agentic/v1/business_snapshot \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"sample_limit":3}'
```

**📈 Executive business cockpit** — revenue, pipeline, cash, inventory &
delivery risk, approvals, plus auto-generated insights.

```bash
curl -s http://localhost:8069/agentic/v1/business_cockpit \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"limit":20}'
```

**Recent normalized business events**

```bash
curl -s http://localhost:8069/agentic/v1/business_events \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"limit":10,"since_days":7}'
```

**Safe action planning (no execution)**

```bash
curl -s http://localhost:8069/agentic/v1/action_plan \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"goal":"Create a CRM lead for Acme with expected revenue 5000"}'
```

**📚 OKF knowledge bundle export**

```bash
curl -s http://localhost:8069/agentic/v1/okf_bundle \
  -H "authorization: Bearer dev-agentic-key" \
  -H "content-type: application/json" \
  -d '{"sample_limit":3}'
```

The response is an **Open Knowledge Format v0.1** bundle of Markdown file
entries. Agents can materialize the returned `files[].path` / `files[].content`
values into a directory, index them, or publish them as an agent-readable
business wiki.

---

## 🎬 Demo

Seed a full **CRM / Sales / Inventory / Accounting** demo company:

```bash
scripts/seed-demo-company
```

Then open the 📱 mobile approval UI:

```text
http://localhost:8069/agentic/ui/approvals
```

Log in with Odoo `admin` / `admin`. The full demo runbook lives in
[`docs/DEMO.md`](docs/DEMO.md).

---

## 🗺️ Roadmap & Rituals

- 📋 Working backlog → [`BACKLOG.md`](BACKLOG.md)
- 🔁 Daily implementation ritual → [`docs/DAILY_LOOP.md`](docs/DAILY_LOOP.md)

---

## 📄 License

Published under the **GNU Lesser General Public License v3.0** (`LGPL-3.0`),
matching the Odoo module manifest. See [`LICENSE`](LICENSE).
