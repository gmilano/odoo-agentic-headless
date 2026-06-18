import json
import os
from datetime import timedelta

from odoo import fields as odoo_fields
from odoo import http
from odoo.http import Response, request


MAX_LIMIT = 200
SNAPSHOT_SAMPLE_LIMIT = 3
MAX_LOG_JSON_CHARS = 20000
MAX_AUDIT_LOG_LIMIT = 200
AUDIT_LOG_RETENTION_DAYS = 90

ALLOWED_OPERATIONS = [
    {
        "name": "schema",
        "endpoint": "/agentic/v1/schema",
        "method": "POST",
        "risk": "low",
        "effect": "read_metadata",
    },
    {
        "name": "search_read",
        "endpoint": "/agentic/v1/search_read",
        "method": "POST",
        "risk": "low",
        "effect": "read_records",
    },
    {
        "name": "business_snapshot",
        "endpoint": "/agentic/v1/business_snapshot",
        "method": "GET|POST",
        "risk": "low",
        "effect": "read_business_summary",
    },
    {
        "name": "business_events",
        "endpoint": "/agentic/v1/business_events",
        "method": "GET|POST",
        "risk": "low",
        "effect": "read_normalized_recent_changes",
    },
    {
        "name": "action_plan",
        "endpoint": "/agentic/v1/action_plan",
        "method": "POST",
        "risk": "low",
        "effect": "plan_operations_without_execution",
        "executes": False,
    },
    {
        "name": "okf_bundle",
        "endpoint": "/agentic/v1/okf_bundle",
        "method": "GET|POST",
        "risk": "low",
        "effect": "export_agent_readable_okf_markdown_bundle",
        "executes": False,
    },
    {
        "name": "audit_logs",
        "endpoint": "/agentic/v1/audit_logs",
        "method": "GET|POST",
        "risk": "low",
        "effect": "review_filtered_agentic_api_audit_trail",
        "executes": False,
    },
    {
        "name": "create",
        "endpoint": "/agentic/v1/create",
        "method": "POST",
        "risk": "medium",
        "effect": "create_record",
        "requires_approval": False,
    },
    {
        "name": "write",
        "endpoint": "/agentic/v1/write",
        "method": "POST",
        "risk": "medium",
        "effect": "update_records",
        "requires_approval": False,
    },
    {
        "name": "call",
        "endpoint": "/agentic/v1/call",
        "method": "POST",
        "risk": "high",
        "effect": "invoke_model_method",
        "requires_approval": True,
    },
]

RISKY_OPERATIONS = [
    {
        "name": "call",
        "reason": "Arbitrary public model methods can trigger workflows, posting, confirmations, or integrations.",
        "current_guardrail": "Private methods are blocked. Approval queue is not implemented yet.",
    },
    {
        "name": "write_financial_records",
        "reason": "Financial records can affect invoices, journals, tax reports, and external accounting state.",
        "current_guardrail": "Use schema/search_read first; explicit approval will be added in AH-0010/AH-0106.",
    },
    {
        "name": "confirm_or_cancel_documents",
        "reason": "State-changing workflow methods can commit sales, purchase, stock, manufacturing, or accounting actions.",
        "current_guardrail": "Treat state transition methods as approval-required through the agent adapter.",
    },
]

ERP_MODEL_CATALOG = [
    ("res.partner", "contacts", ["name", "email", "phone", "is_company"]),
    ("res.company", "companies", ["name", "email", "phone", "currency_id"]),
    ("res.users", "users", ["name", "login", "company_id"]),
    ("crm.lead", "crm_pipeline", ["name", "stage_id", "expected_revenue", "probability"]),
    ("sale.order", "sales", ["name", "partner_id", "state", "amount_total", "date_order"]),
    ("purchase.order", "purchasing", ["name", "partner_id", "state", "amount_total", "date_order"]),
    ("stock.picking", "inventory", ["name", "partner_id", "state", "scheduled_date"]),
    ("account.move", "accounting", ["name", "partner_id", "state", "move_type", "amount_total"]),
    ("project.project", "projects", ["name", "partner_id", "user_id"]),
    ("project.task", "tasks", ["name", "project_id", "stage_id", "user_ids"]),
    ("hr.employee", "people", ["name", "work_email", "department_id", "job_id"]),
    ("mrp.production", "manufacturing", ["name", "product_id", "state", "product_qty"]),
    ("helpdesk.ticket", "support", ["name", "partner_id", "stage_id", "priority"]),
]

BUSINESS_EVENT_MODEL_CATALOG = [
    ("crm.lead", "crm", ["name", "stage_id", "expected_revenue", "probability", "create_date", "write_date", "create_uid", "write_uid"]),
    ("sale.order", "sales", ["name", "partner_id", "state", "amount_total", "date_order", "create_date", "write_date", "create_uid", "write_uid"]),
    ("stock.picking", "inventory", ["name", "partner_id", "state", "scheduled_date", "create_date", "write_date", "create_uid", "write_uid"]),
    ("account.move", "accounting", ["name", "partner_id", "state", "move_type", "amount_total", "create_date", "write_date", "create_uid", "write_uid"]),
    ("project.project", "projects", ["name", "partner_id", "user_id", "create_date", "write_date", "create_uid", "write_uid"]),
    ("project.task", "projects", ["name", "project_id", "stage_id", "user_ids", "create_date", "write_date", "create_uid", "write_uid"]),
]

ACTION_PLAN_TEMPLATES = [
    {
        "intent": "create_crm_lead",
        "title": "Create a CRM opportunity",
        "keywords": ["lead", "opportunity", "crm", "pipeline", "prospect"],
        "required_models": ["crm.lead", "res.partner"],
        "required_inputs": ["lead name or opportunity summary"],
        "optional_inputs": ["customer", "expected revenue", "probability", "salesperson"],
        "operations": [
            {
                "operation": "search_read",
                "endpoint": "/agentic/v1/search_read",
                "model": "res.partner",
                "purpose": "Find an existing customer/contact before linking the lead.",
                "payload_template": {
                    "model": "res.partner",
                    "domain": [["name", "ilike", "<customer name>"]],
                    "fields": ["name", "email", "phone", "is_company"],
                    "limit": 5,
                },
            },
            {
                "operation": "create",
                "endpoint": "/agentic/v1/create",
                "model": "crm.lead",
                "purpose": "Create the opportunity after a human or agent fills confirmed values.",
                "payload_template": {
                    "model": "crm.lead",
                    "values": {
                        "name": "<opportunity summary>",
                        "partner_id": "<res.partner id, optional>",
                        "expected_revenue": "<amount, optional>",
                        "probability": "<0-100, optional>",
                    },
                },
            },
        ],
    },
    {
        "intent": "create_sales_quotation",
        "title": "Create a sales quotation",
        "keywords": ["quote", "quotation", "sales order", "sale order", "proposal", "sell"],
        "required_models": ["sale.order", "res.partner"],
        "required_inputs": ["customer", "order lines or commercial summary"],
        "optional_inputs": ["validity date", "pricelist", "salesperson"],
        "operations": [
            {
                "operation": "search_read",
                "endpoint": "/agentic/v1/search_read",
                "model": "res.partner",
                "purpose": "Resolve the customer before creating a quotation.",
                "payload_template": {
                    "model": "res.partner",
                    "domain": [["name", "ilike", "<customer name>"]],
                    "fields": ["name", "email", "phone", "is_company"],
                    "limit": 5,
                },
            },
            {
                "operation": "create",
                "endpoint": "/agentic/v1/create",
                "model": "sale.order",
                "purpose": "Create a draft quotation. Order lines should be added only after product and pricing lookup.",
                "payload_template": {
                    "model": "sale.order",
                    "values": {
                        "partner_id": "<res.partner id>",
                    },
                },
            },
        ],
    },
    {
        "intent": "review_financial_documents",
        "title": "Review invoices or accounting moves",
        "keywords": ["invoice", "bill", "accounting", "payment", "overdue", "cash", "receivable", "payable"],
        "required_models": ["account.move"],
        "required_inputs": [],
        "optional_inputs": ["customer/vendor", "date range", "state", "move type"],
        "operations": [
            {
                "operation": "search_read",
                "endpoint": "/agentic/v1/search_read",
                "model": "account.move",
                "purpose": "Inspect financial documents before proposing any state-changing action.",
                "payload_template": {
                    "model": "account.move",
                    "domain": [["move_type", "in", ["out_invoice", "in_invoice"]]],
                    "fields": ["name", "partner_id", "state", "move_type", "amount_total"],
                    "limit": 20,
                    "order": "date desc",
                },
            },
        ],
        "risk": "high",
        "approval_required": True,
    },
    {
        "intent": "review_inventory_deliveries",
        "title": "Review delivery or inventory work",
        "keywords": ["inventory", "stock", "delivery", "picking", "warehouse", "shipment", "ship"],
        "required_models": ["stock.picking"],
        "required_inputs": [],
        "optional_inputs": ["customer/vendor", "scheduled date", "state"],
        "operations": [
            {
                "operation": "search_read",
                "endpoint": "/agentic/v1/search_read",
                "model": "stock.picking",
                "purpose": "Inspect transfers before proposing confirmations, cancellations, or scheduling changes.",
                "payload_template": {
                    "model": "stock.picking",
                    "domain": [],
                    "fields": ["name", "partner_id", "state", "scheduled_date"],
                    "limit": 20,
                    "order": "scheduled_date asc",
                },
            },
        ],
        "risk": "medium",
        "approval_required": True,
    },
    {
        "intent": "review_business_activity",
        "title": "Understand recent business activity",
        "keywords": ["what changed", "recent", "activity", "summary", "status", "snapshot", "business"],
        "required_models": [],
        "required_inputs": [],
        "optional_inputs": ["time window"],
        "operations": [
            {
                "operation": "business_snapshot",
                "endpoint": "/agentic/v1/business_snapshot",
                "model": None,
                "purpose": "Read current ERP surface, counts, samples, and trend memory.",
                "payload_template": {
                    "sample_limit": 3,
                },
            },
            {
                "operation": "business_events",
                "endpoint": "/agentic/v1/business_events",
                "model": None,
                "purpose": "Read normalized recent operational changes.",
                "payload_template": {
                    "since_days": 7,
                    "limit": 50,
                },
            },
        ],
    },
]


class AgenticHeadlessController(http.Controller):
    @http.route(
        "/agentic/v1/health",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def health(self, **_kwargs):
        return json_response({
            "ok": True,
            "service": "agentic_headless",
            "database": getattr(request.env.cr, "dbname", None),
        })

    @http.route(
        "/agentic/v1/schema",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def schema(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        model_name = required_string(payload, "model")
        if not model_name:
            return json_error("missing_model", "Expected JSON field 'model'.", 400)

        model = get_model(model_name)
        if isinstance(model, Response):
            return model

        attributes = payload.get("attributes") or [
            "string",
            "type",
            "required",
            "readonly",
            "relation",
            "selection",
            "help",
        ]
        fields = model.fields_get(allfields=payload.get("fields"), attributes=attributes)
        return json_response({
            "ok": True,
            "model": model_name,
            "fields": fields,
        })

    @http.route(
        "/agentic/v1/search_read",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def search_read(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        model = get_model(required_string(payload, "model"))
        if isinstance(model, Response):
            return model

        domain = payload.get("domain") or []
        fields = payload.get("fields")
        limit = bounded_limit(payload.get("limit", 80))
        offset = int(payload.get("offset") or 0)
        order = payload.get("order")

        rows = model.search_read(
            domain=domain,
            fields=fields,
            offset=offset,
            limit=limit,
            order=order,
        )
        return json_response({
            "ok": True,
            "count": len(rows),
            "rows": rows,
        })

    @http.route(
        "/agentic/v1/business_snapshot",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        cors="*",
    )
    def business_snapshot(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        sample_limit = min(bounded_limit(payload.get("sample_limit", SNAPSHOT_SAMPLE_LIMIT)), 10)
        modules = installed_modules()
        surface = [model_snapshot(model, domain, fields, sample_limit) for model, domain, fields in ERP_MODEL_CATALOG]
        available = [item for item in surface if item["available"]]

        return json_response({
            "ok": True,
            "database": getattr(request.env.cr, "dbname", None),
            "company": company_snapshot(),
            "installed_modules": {
                "count": len(modules),
                "names": modules[:80],
            },
            "erp_surface": surface,
            "trend_memory": business_snapshot_trend(surface),
            "insights": business_insights(available, modules),
        })

    @http.route(
        "/agentic/v1/business_events",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        cors="*",
    )
    def business_events(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        limit = min(bounded_limit(payload.get("limit", 50)), 100)
        since_days = bounded_days(payload.get("since_days", 7))
        since = odoo_fields.Datetime.now() - timedelta(days=since_days)

        model_results = [
            normalized_model_events(model, domain, requested_fields, since, limit)
            for model, domain, requested_fields in BUSINESS_EVENT_MODEL_CATALOG
        ]
        events = sorted(
            [
                event
                for result in model_results
                for event in result["events"]
            ],
            key=lambda event: event["occurred_at"] or "",
            reverse=True,
        )[:limit]

        return json_response({
            "ok": True,
            "database": getattr(request.env.cr, "dbname", None),
            "window": {
                "since_days": since_days,
                "since": odoo_fields.Datetime.to_string(since),
                "limit": limit,
            },
            "coverage": [
                {
                    "domain": result["domain"],
                    "model": result["model"],
                    "available": result["available"],
                    "count": result["count"],
                }
                for result in model_results
            ],
            "count": len(events),
            "events": events,
            "insights": business_event_insights(events, model_results),
        })

    @http.route(
        "/agentic/v1/capabilities",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        cors="*",
    )
    def capabilities(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        modules = installed_modules()
        domains = [domain_capability(model, domain, fields) for model, domain, fields in ERP_MODEL_CATALOG]
        available_domains = [domain for domain in domains if domain["available"]]

        return json_response({
            "ok": True,
            "database": getattr(request.env.cr, "dbname", None),
            "service": {
                "name": "agentic_headless",
                "version": addon_version(),
                "api_version": "v1",
            },
            "installed_modules": {
                "count": len(modules),
                "names": modules[:120],
            },
            "models": {
                "tracked_count": len(domains),
                "available_count": len(available_domains),
                "domains": domains,
            },
            "allowed_operations": ALLOWED_OPERATIONS,
            "risky_operations": RISKY_OPERATIONS,
            "guardrails": {
                "authentication": "Bearer token via AGENTIC_HEADLESS_API_KEY",
                "private_methods_blocked": True,
                "max_search_read_limit": MAX_LIMIT,
                "audit_log_model": model_exists("agentic.request.log"),
                "audit_log_query_filters": True,
                "audit_log_retention_days": AUDIT_LOG_RETENTION_DAYS,
                "approval_queue": False,
            },
            "next_safety_gaps": [
                "Classify write/call payloads before execution.",
                "Require explicit approval for financial and destructive workflow transitions.",
            ],
        })

    @http.route(
        "/agentic/v1/audit_logs",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        cors="*",
    )
    def audit_logs(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        if not model_exists("agentic.request.log") or not table_exists("agentic_request_log"):
            return json_error("audit_log_unavailable", "The agentic.request.log model is not installed yet.", 503)

        payload = read_json()
        limit = min(bounded_limit(payload.get("limit", 50)), MAX_AUDIT_LOG_LIMIT)
        offset = bounded_offset(payload.get("offset"))
        include_payloads = optional_bool(payload.get("include_payloads"))
        if include_payloads is None:
            include_payloads = False
        domain = audit_log_domain(payload)
        log_model = request.env["agentic.request.log"].sudo()
        logs = log_model.search(domain, order="create_date desc, id desc", limit=limit, offset=offset)

        return json_response({
            "ok": True,
            "database": getattr(request.env.cr, "dbname", None),
            "filters": audit_log_filter_summary(payload, domain, limit, offset, include_payloads),
            "retention_policy": audit_log_retention_policy(log_model),
            "count": len(logs),
            "total_matching": log_model.search_count(domain),
            "logs": [serialize_audit_log(log, include_payloads) for log in logs],
        })

    @http.route(
        "/agentic/v1/action_plan",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def action_plan(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        goal = required_string(payload, "goal")
        if not goal:
            return json_error("missing_goal", "Expected JSON field 'goal'.", 400)

        template = select_action_plan_template(goal)
        unavailable_models = unavailable_required_models(template)
        risk = template.get("risk") or inferred_plan_risk(template)
        approval_required = bool(template.get("approval_required") or risk in {"medium", "high"})
        missing_inputs = list(template.get("required_inputs") or [])
        candidate_values = payload.get("candidate_values") if isinstance(payload.get("candidate_values"), dict) else {}

        return json_response({
            "ok": True,
            "goal": goal,
            "executes": False,
            "plan": {
                "intent": template["intent"],
                "title": template["title"],
                "risk": risk,
                "approval_required": approval_required,
                "status": "blocked" if unavailable_models else "draft",
                "confidence": action_plan_confidence(goal, template),
                "required_inputs": missing_inputs,
                "optional_inputs": template.get("optional_inputs") or [],
                "candidate_values": candidate_values,
                "unavailable_models": unavailable_models,
                "operations": template["operations"],
                "execution_contract": {
                    "execute_endpoint": "/agentic/v1/execute_plan",
                    "available": False,
                    "current_next_step": "Review and fill payload_template values, then execute manually through existing endpoints until AH-0010 exists.",
                },
            },
            "guardrails": [
                "This endpoint never creates, writes, confirms, posts, cancels, or calls model methods.",
                "Financial, inventory workflow, and arbitrary method actions remain approval-required.",
                "Use search_read/schema/capabilities to resolve IDs and validate fields before execution.",
            ],
        })

    @http.route(
        "/agentic/v1/okf_bundle",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        cors="*",
    )
    def okf_bundle(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        sample_limit = min(bounded_limit(payload.get("sample_limit", SNAPSHOT_SAMPLE_LIMIT)), 10)
        bundle = build_okf_bundle(sample_limit)
        return json_response({
            "ok": True,
            "okf_version": "0.1",
            "bundle_name": bundle["bundle_name"],
            "generated_at": bundle["generated_at"],
            "file_count": len(bundle["files"]),
            "files": bundle["files"],
            "usage": {
                "format": "Each file entry contains an OKF markdown document. Write the paths and contents to a directory to materialize the bundle.",
                "spec": "https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md",
            },
        })

    @http.route(
        "/agentic/v1/create",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def create(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        model = get_model(required_string(payload, "model"))
        if isinstance(model, Response):
            return model

        values = payload.get("values")
        if not isinstance(values, dict):
            return json_error("invalid_values", "Expected object field 'values'.", 400)

        record = model.create(values)
        return json_response({
            "ok": True,
            "id": record.id,
            "display_name": record.display_name,
        })

    @http.route(
        "/agentic/v1/write",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def write(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        model = get_model(required_string(payload, "model"))
        if isinstance(model, Response):
            return model

        ids = payload.get("ids")
        values = payload.get("values")
        if not isinstance(ids, list) or not all(isinstance(item, int) for item in ids):
            return json_error("invalid_ids", "Expected integer list field 'ids'.", 400)
        if not isinstance(values, dict):
            return json_error("invalid_values", "Expected object field 'values'.", 400)

        records = model.browse(ids).exists()
        records.write(values)
        return json_response({
            "ok": True,
            "updated": len(records),
            "ids": records.ids,
        })

    @http.route(
        "/agentic/v1/call",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def call(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        model = get_model(required_string(payload, "model"))
        if isinstance(model, Response):
            return model

        method = required_string(payload, "method")
        if not method:
            return json_error("missing_method", "Expected JSON field 'method'.", 400)
        if method.startswith("_"):
            return json_error("private_method", "Private model methods are not callable.", 403)

        args = payload.get("args") or []
        kwargs = payload.get("kwargs") or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return json_error("invalid_call", "Expected 'args' list and 'kwargs' object.", 400)

        result = getattr(model, method)(*args, **kwargs)
        return json_response({
            "ok": True,
            "result": jsonable(result),
        })


def require_api_key():
    expected = os.getenv("AGENTIC_HEADLESS_API_KEY", "").strip()
    if not expected:
        return json_error(
            "api_key_not_configured",
            "Set AGENTIC_HEADLESS_API_KEY before enabling the agentic API.",
            503,
        )

    header = request.httprequest.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip()
    if token != expected:
        return json_error("unauthorized", "Invalid or missing bearer token.", 401)
    return None


def read_json():
    raw = request.httprequest.get_data(as_text=True) or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def required_string(payload, key):
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def get_model(model_name):
    if not model_name:
        return json_error("missing_model", "Expected JSON field 'model'.", 400)
    if model_name not in request.env.registry.models:
        return json_error("unknown_model", f"Unknown model: {model_name}", 404)
    return request.env[model_name].sudo().with_context(agentic_headless=True)


def model_exists(model_name):
    return model_name in request.env.registry.models


def installed_modules():
    if not model_exists("ir.module.module"):
        return []
    modules = request.env["ir.module.module"].sudo().search_read(
        domain=[("state", "=", "installed")],
        fields=["name"],
        order="name",
        limit=500,
    )
    return [item["name"] for item in modules]


def company_snapshot():
    if not model_exists("res.company"):
        return None
    company = request.env.company.sudo()
    return {
        "id": company.id,
        "name": company.name,
        "currency": company.currency_id.name if company.currency_id else None,
        "country": company.country_id.name if company.country_id else None,
    }


def model_snapshot(model_name, domain_name, requested_fields, sample_limit):
    if not model_exists(model_name):
        return {
            "model": model_name,
            "domain": domain_name,
            "available": False,
            "count": 0,
            "sample": [],
        }

    model = request.env[model_name].sudo().with_context(agentic_headless=True)
    fields = available_fields(model, requested_fields)
    count = model.search_count([])
    sample = []
    if fields and count:
        sample = model.search_read(
            domain=[],
            fields=fields,
            limit=sample_limit,
            order=default_order(model),
        )
    return {
        "model": model_name,
        "domain": domain_name,
        "available": True,
        "count": count,
        "fields": fields,
        "sample": sample,
    }


def normalized_model_events(model_name, domain_name, requested_fields, since, limit):
    if not model_exists(model_name):
        return {
            "model": model_name,
            "domain": domain_name,
            "available": False,
            "count": 0,
            "events": [],
        }

    model = request.env[model_name].sudo().with_context(agentic_headless=True)
    all_fields = model.fields_get(attributes=["type"])
    date_domain = recent_activity_domain(all_fields, since)
    if not date_domain:
        return {
            "model": model_name,
            "domain": domain_name,
            "available": True,
            "count": 0,
            "events": [],
        }

    fields = available_fields(model, requested_fields)
    if "display_name" not in fields:
        fields.append("display_name")
    rows = model.search_read(
        domain=date_domain,
        fields=fields,
        limit=limit,
        order=default_order(model),
    )
    return {
        "model": model_name,
        "domain": domain_name,
        "available": True,
        "count": len(rows),
        "events": [
            normalize_business_event(model_name, domain_name, row)
            for row in rows
        ],
    }


def build_okf_bundle(sample_limit):
    generated_at = odoo_fields.Datetime.now().replace(microsecond=0).isoformat() + "Z"
    modules = installed_modules()
    surface = [model_snapshot(model, domain, fields, sample_limit) for model, domain, fields in ERP_MODEL_CATALOG]
    domains = [domain_capability(model, domain, fields) for model, domain, fields in ERP_MODEL_CATALOG]
    company = company_snapshot()
    files = [
        okf_file(
            "index.md",
            okf_root_index(generated_at, company, surface),
        ),
        okf_file(
            "log.md",
            okf_log(generated_at),
        ),
        okf_file(
            "business/company.md",
            okf_concept(
                {
                    "type": "Odoo Company",
                    "title": company.get("name") if company else "Odoo Company",
                    "description": "Current company context exposed by the agentic Odoo layer.",
                    "tags": ["odoo", "company", "business-context"],
                    "timestamp": generated_at,
                },
                okf_company_body(company),
            ),
        ),
        okf_file(
            "business/capabilities.md",
            okf_concept(
                {
                    "type": "Agentic API Capability Map",
                    "title": "Agentic Headless Capabilities",
                    "description": "Installed modules, tracked domains, operations, and guardrails available to agents.",
                    "resource": "/agentic/v1/capabilities",
                    "tags": ["odoo", "agentic-api", "capabilities"],
                    "timestamp": generated_at,
                },
                okf_capabilities_body(modules, domains),
            ),
        ),
    ]

    files.extend(okf_domain_file(item, generated_at) for item in surface)
    files.extend(okf_operation_file(operation, generated_at) for operation in ALLOWED_OPERATIONS)
    return {
        "bundle_name": "odoo-agentic-headless",
        "generated_at": generated_at,
        "files": files,
    }


def okf_file(path, content):
    return {
        "path": path,
        "content_type": "text/markdown; charset=utf-8",
        "content": content,
    }


def okf_root_index(generated_at, company, surface):
    company_name = company.get("name") if company else "Unknown company"
    available = [item for item in surface if item["available"]]
    return "\n".join([
        "---",
        'okf_version: "0.1"',
        "---",
        "# Odoo Agentic Headless Knowledge Bundle",
        "",
        f"Generated at `{generated_at}` from the Odoo database for **{company_name}**.",
        "",
        "# Business",
        "",
        "* [Company](business/company.md) - Current Odoo company context.",
        "* [Capabilities](business/capabilities.md) - Agent-facing API operations, models, and guardrails.",
        "",
        "# Domains",
        "",
        *[
            f"* [{item['domain']}](domains/{item['domain']}.md) - `{item['model']}` with {item.get('count', 0)} records."
            for item in available
        ],
        "",
        "# Operations",
        "",
        *[
            f"* [{operation['name']}](operations/{operation['name']}.md) - {operation['effect']}."
            for operation in ALLOWED_OPERATIONS
        ],
        "",
    ])


def okf_log(generated_at):
    day = generated_at.split("T")[0]
    return "\n".join([
        "# Bundle Update Log",
        "",
        f"## {day}",
        "",
        "* **Creation**: Generated OKF v0.1 bundle from `/agentic/v1/okf_bundle`.",
        "",
    ])


def okf_concept(frontmatter, body):
    return "\n".join([
        "---",
        *[yaml_line(key, value) for key, value in frontmatter.items() if value not in (None, "", [])],
        "---",
        "",
        body.strip(),
        "",
    ])


def yaml_line(key, value):
    if isinstance(value, list):
        rendered = ", ".join(yaml_scalar(item) for item in value)
        return f"{key}: [{rendered}]"
    return f"{key}: {yaml_scalar(value)}"


def yaml_scalar(value):
    return json.dumps(str(value))


def okf_company_body(company):
    if not company:
        return "# Summary\n\nNo active company context was available."
    return "\n".join([
        "# Summary",
        "",
        f"* Company ID: `{company.get('id')}`",
        f"* Name: {company.get('name')}",
        f"* Currency: {company.get('currency') or 'Unknown'}",
        f"* Country: {company.get('country') or 'Unknown'}",
        "",
        "# Related Concepts",
        "",
        "* [Capabilities](/business/capabilities.md)",
    ])


def okf_capabilities_body(modules, domains):
    available = [domain for domain in domains if domain["available"]]
    unavailable = [domain for domain in domains if not domain["available"]]
    return "\n".join([
        "# Installed Modules",
        "",
        f"{len(modules)} modules installed.",
        "",
        ", ".join(f"`{module}`" for module in modules[:120]) or "No installed modules detected.",
        "",
        "# Available Domains",
        "",
        *[
            f"* [{domain['domain']}](/domains/{domain['domain']}.md) - `{domain['model']}`"
            for domain in available
        ],
        "",
        "# Unavailable Tracked Domains",
        "",
        *[
            f"* `{domain['model']}` ({domain['domain']})"
            for domain in unavailable
        ],
        "",
        "# Guardrails",
        "",
        "* API calls require a bearer token via `AGENTIC_HEADLESS_API_KEY`.",
        "* Private model methods are blocked.",
        f"* Search/read limits are capped at `{MAX_LIMIT}` records.",
        "* Financial and workflow-changing operations should require explicit approval before execution.",
    ])


def okf_domain_file(item, generated_at):
    return okf_file(
        f"domains/{item['domain']}.md",
        okf_concept(
            {
                "type": "Odoo Model Domain",
                "title": item["domain"],
                "description": f"Odoo model `{item['model']}` exposed as the {item['domain']} business domain.",
                "resource": f"odoo://model/{item['model']}",
                "tags": ["odoo", "erp-domain", item["domain"]],
                "timestamp": generated_at,
            },
            okf_domain_body(item),
        ),
    )


def okf_domain_body(item):
    lines = [
        "# Summary",
        "",
        f"* Model: `{item['model']}`",
        f"* Available: `{str(item['available']).lower()}`",
        f"* Record count: `{item.get('count', 0)}`",
        "",
    ]
    if item.get("fields"):
        lines.extend([
            "# Schema",
            "",
            "| Field |",
            "|-------|",
            *[f"| `{field}` |" for field in item["fields"]],
            "",
        ])
    if item.get("sample"):
        lines.extend([
            "# Examples",
            "",
            "```json",
            json.dumps(item["sample"], indent=2, default=str),
            "```",
            "",
        ])
    lines.extend([
        "# Related Concepts",
        "",
        "* [Capabilities](/business/capabilities.md)",
    ])
    return "\n".join(lines)


def okf_operation_file(operation, generated_at):
    return okf_file(
        f"operations/{operation['name']}.md",
        okf_concept(
            {
                "type": "Agentic API Endpoint",
                "title": operation["name"],
                "description": operation["effect"],
                "resource": operation["endpoint"],
                "tags": ["odoo", "agentic-api", operation["risk"]],
                "timestamp": generated_at,
            },
            okf_operation_body(operation),
        ),
    )


def okf_operation_body(operation):
    lines = [
        "# Summary",
        "",
        f"* Endpoint: `{operation['endpoint']}`",
        f"* Method: `{operation['method']}`",
        f"* Risk: `{operation['risk']}`",
        f"* Effect: `{operation['effect']}`",
        f"* Executes changes: `{str(operation.get('executes', operation['risk'] != 'low')).lower()}`",
    ]
    if "requires_approval" in operation:
        lines.append(f"* Requires approval: `{str(operation['requires_approval']).lower()}`")
    lines.extend([
        "",
        "# Related Concepts",
        "",
        "* [Capabilities](/business/capabilities.md)",
    ])
    return "\n".join(lines)


def recent_activity_domain(all_fields, since):
    since_value = odoo_fields.Datetime.to_string(since)
    has_create = "create_date" in all_fields
    has_write = "write_date" in all_fields
    if has_create and has_write:
        return ["|", ("create_date", ">=", since_value), ("write_date", ">=", since_value)]
    if has_write:
        return [("write_date", ">=", since_value)]
    if has_create:
        return [("create_date", ">=", since_value)]
    return []


def normalize_business_event(model_name, domain_name, row):
    create_date = row.get("create_date")
    write_date = row.get("write_date")
    occurred_at = write_date or create_date
    event_type = "created"
    actor = row.get("create_uid")
    if write_date and write_date != create_date:
        event_type = "updated"
        actor = row.get("write_uid") or actor

    return {
        "id": f"{model_name}:{row.get('id')}:{event_type}:{occurred_at}",
        "domain": domain_name,
        "model": model_name,
        "record_id": row.get("id"),
        "record_name": row.get("display_name") or row.get("name"),
        "event_type": event_type,
        "occurred_at": occurred_at,
        "actor": relational_value(actor),
        "summary": business_event_summary(domain_name, row, event_type),
        "signals": business_event_signals(row),
    }


def business_event_summary(domain_name, row, event_type):
    name = row.get("display_name") or row.get("name") or f"record {row.get('id')}"
    state = plain_relational_value(row.get("state") or row.get("stage_id"))
    amount = row.get("amount_total") or row.get("expected_revenue")
    pieces = [f"{domain_name} {event_type}: {name}"]
    if state:
        pieces.append(f"state/stage={state}")
    if amount:
        pieces.append(f"amount={amount}")
    return "; ".join(pieces)


def business_event_signals(row):
    keys = [
        "partner_id",
        "state",
        "stage_id",
        "amount_total",
        "expected_revenue",
        "probability",
        "move_type",
        "scheduled_date",
        "date_order",
        "project_id",
        "user_id",
        "user_ids",
    ]
    return {
        key: jsonable(row.get(key))
        for key in keys
        if key in row and row.get(key) not in (None, False, [], "")
    }


def relational_value(value):
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return {
            "id": value[0],
            "name": value[1],
        }
    return value


def plain_relational_value(value):
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return value[1]
    return value


def business_event_insights(events, model_results):
    available = [result for result in model_results if result["available"]]
    inactive = [result["domain"] for result in available if not result["count"]]
    if not events:
        return [{
            "level": "quiet",
            "title": "No recent operational events",
            "detail": "No tracked CRM, Sales, Inventory, Accounting, or Project records changed in the selected window.",
        }]

    domains = {}
    for event in events:
        domains[event["domain"]] = domains.get(event["domain"], 0) + 1

    insights = [{
        "level": "activity",
        "title": "Recent business activity is available for agent review",
        "detail": ", ".join(f"{domain}: {count}" for domain, count in sorted(domains.items())),
    }]
    if inactive:
        insights.append({
            "level": "coverage",
            "title": "Some installed operational domains are quiet",
            "detail": f"No recent tracked changes for: {', '.join(sorted(set(inactive)))}.",
        })
    return insights


def audit_log_domain(payload):
    domain = []
    for field_name in ["endpoint", "operation", "model_name", "error_code"]:
        value = required_string(payload, field_name)
        if value:
            domain.append((field_name, "=", value))

    for field_name in ["ok", "authenticated"]:
        value = optional_bool(payload.get(field_name))
        if value is not None:
            domain.append((field_name, "=", value))

    try:
        status_min = int(payload.get("status_min")) if payload.get("status_min") is not None else None
    except (TypeError, ValueError):
        status_min = None
    try:
        status_max = int(payload.get("status_max")) if payload.get("status_max") is not None else None
    except (TypeError, ValueError):
        status_max = None
    if status_min is not None:
        domain.append(("status_code", ">=", status_min))
    if status_max is not None:
        domain.append(("status_code", "<=", status_max))

    since_days = payload.get("since_days")
    if since_days is not None:
        since = odoo_fields.Datetime.now() - timedelta(days=bounded_days(since_days))
        domain.append(("create_date", ">=", odoo_fields.Datetime.to_string(since)))

    return domain


def optional_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    return None


def audit_log_filter_summary(payload, domain, limit, offset, include_payloads):
    return {
        "endpoint": required_string(payload, "endpoint") or None,
        "operation": required_string(payload, "operation") or None,
        "model_name": required_string(payload, "model_name") or None,
        "error_code": required_string(payload, "error_code") or None,
        "ok": optional_bool(payload.get("ok")),
        "authenticated": optional_bool(payload.get("authenticated")),
        "status_min": payload.get("status_min"),
        "status_max": payload.get("status_max"),
        "since_days": payload.get("since_days"),
        "limit": limit,
        "offset": offset,
        "include_payloads": include_payloads,
        "domain": domain,
    }


def audit_log_retention_policy(log_model):
    cutoff = odoo_fields.Datetime.now() - timedelta(days=AUDIT_LOG_RETENTION_DAYS)
    cutoff_string = odoo_fields.Datetime.to_string(cutoff)
    expired_count = log_model.search_count([("create_date", "<", cutoff_string)])
    return {
        "retention_days": AUDIT_LOG_RETENTION_DAYS,
        "cutoff": cutoff_string,
        "expired_count": expired_count,
        "control": "Review expired_count here before adding a scheduled pruning job or explicit prune endpoint.",
    }


def serialize_audit_log(log, include_payloads):
    item = {
        "id": log.id,
        "created_at": log.create_date,
        "endpoint": log.endpoint,
        "method": log.method,
        "operation": log.operation,
        "model": log.model_name,
        "status_code": log.status_code,
        "ok": log.ok,
        "error_code": log.error_code,
        "authenticated": log.authenticated,
        "remote_addr": log.remote_addr,
        "user_agent": log.user_agent,
    }
    if include_payloads:
        item.update({
            "payload": parse_logged_json(log.payload_json),
            "response": parse_logged_json(log.response_json),
        })
    return item


def parse_logged_json(value):
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return value


def domain_capability(model_name, domain_name, requested_fields):
    if not model_exists(model_name):
        return {
            "domain": domain_name,
            "model": model_name,
            "available": False,
            "readable": False,
            "writable": False,
            "fields": [],
        }

    model = request.env[model_name].sudo().with_context(agentic_headless=True)
    fields = model.fields_get(attributes=["type", "readonly", "required", "relation"])
    field_names = available_fields(model, requested_fields)
    return {
        "domain": domain_name,
        "model": model_name,
        "available": True,
        "readable": True,
        "writable": has_writable_fields(fields),
        "fields": [
            {
                "name": field_name,
                "type": fields[field_name].get("type"),
                "relation": fields[field_name].get("relation"),
                "required": bool(fields[field_name].get("required")),
                "readonly": bool(fields[field_name].get("readonly")),
            }
            for field_name in field_names
        ],
    }


def select_action_plan_template(goal):
    normalized_goal = normalize_text(goal)
    scored = [
        (keyword_score(normalized_goal, template.get("keywords") or []), template)
        for template in ACTION_PLAN_TEMPLATES
    ]
    score, template = max(scored, key=lambda item: item[0])
    if score:
        return template
    return {
        "intent": "generic_review_then_plan",
        "title": "Inspect ERP context before proposing execution",
        "keywords": [],
        "required_models": [],
        "required_inputs": ["specific target record or business object", "desired outcome"],
        "optional_inputs": ["domain", "date range", "constraints"],
        "operations": [
            {
                "operation": "capabilities",
                "endpoint": "/agentic/v1/capabilities",
                "model": None,
                "purpose": "Discover installed domains, writable models, allowed operations, and safety gaps.",
                "payload_template": {},
            },
            {
                "operation": "business_snapshot",
                "endpoint": "/agentic/v1/business_snapshot",
                "model": None,
                "purpose": "Understand the business surface before choosing model-specific operations.",
                "payload_template": {
                    "sample_limit": 3,
                },
            },
        ],
    }


def normalize_text(value):
    return " ".join(str(value or "").lower().split())


def keyword_score(normalized_goal, keywords):
    return sum(1 for keyword in keywords if keyword in normalized_goal)


def unavailable_required_models(template):
    return [
        model_name
        for model_name in template.get("required_models") or []
        if not model_exists(model_name)
    ]


def inferred_plan_risk(template):
    operation_names = {operation["operation"] for operation in template.get("operations") or []}
    models = {operation.get("model") for operation in template.get("operations") or []}
    if "call" in operation_names or "account.move" in models:
        return "high"
    if {"create", "write"} & operation_names or "stock.picking" in models:
        return "medium"
    return "low"


def action_plan_confidence(goal, template):
    if template["intent"] == "generic_review_then_plan":
        return "low"
    score = keyword_score(normalize_text(goal), template.get("keywords") or [])
    if score >= 2:
        return "high"
    return "medium"


def available_fields(model, requested_fields):
    all_fields = model.fields_get(attributes=["string", "type"])
    return [field for field in requested_fields if field in all_fields]


def has_writable_fields(fields):
    return any(
        not field.get("readonly")
        for field in fields.values()
    )


def addon_version():
    if not model_exists("ir.module.module"):
        return None
    module = request.env["ir.module.module"].sudo().search(
        [("name", "=", "agentic_headless")],
        limit=1,
    )
    return module.installed_version if module else None


def default_order(model):
    fields = model.fields_get(attributes=["type"])
    if "write_date" in fields:
        return "write_date desc"
    if "create_date" in fields:
        return "create_date desc"
    return "id desc"


def business_insights(available, modules):
    by_model = {item["model"]: item for item in available}
    insights = []

    partner_count = by_model.get("res.partner", {}).get("count", 0)
    if partner_count <= 2:
        insights.append({
            "level": "setup",
            "title": "Business graph is still mostly empty",
            "detail": "Only demo/base contacts were found. Importing real customers, vendors and employees should be the first ingestion step.",
        })

    absent_domains = [
        domain for model, domain, _fields in ERP_MODEL_CATALOG
        if model not in by_model and domain not in {"contacts", "companies", "users"}
    ]
    if absent_domains:
        insights.append({
            "level": "coverage",
            "title": "ERP surface is not fully installed",
            "detail": f"Missing operational domains: {', '.join(absent_domains[:8])}. Install only the ones needed for the target vertical.",
        })

    if "sale" not in modules and "crm" not in modules:
        insights.append({
            "level": "opportunity",
            "title": "Sales understanding layer is the next obvious wedge",
            "detail": "CRM and Sales are not installed yet. A headless sales cockpit can become the first SAP-replacement demo story.",
        })

    if not insights:
        insights.append({
            "level": "ready",
            "title": "Core ERP surface is available",
            "detail": "The installed models expose enough structure for agentic workflows and executive snapshots.",
        })

    return insights


def business_snapshot_trend(surface):
    previous = previous_business_snapshot()
    if not previous:
        return {
            "available": False,
            "baseline": True,
            "summary": "No previous successful business snapshot was found. This response becomes the trend baseline.",
            "changes": [],
        }

    previous_surface = previous.get("payload", {}).get("erp_surface") or []
    previous_by_model = {
        item.get("model"): item
        for item in previous_surface
        if isinstance(item, dict) and item.get("model")
    }
    changes = []
    net_count_delta = 0

    for item in surface:
        prior = previous_by_model.get(item["model"], {})
        previous_count = int(prior.get("count") or 0)
        current_count = int(item.get("count") or 0)
        delta = current_count - previous_count
        net_count_delta += delta
        availability_changed = bool(prior.get("available")) != bool(item.get("available"))
        if delta or availability_changed:
            changes.append({
                "domain": item["domain"],
                "model": item["model"],
                "previous_count": previous_count,
                "current_count": current_count,
                "delta": delta,
                "availability_changed": availability_changed,
            })

    return {
        "available": True,
        "baseline": False,
        "compared_to": {
            "request_log_id": previous["id"],
            "captured_at": previous["create_date"],
        },
        "changed_domains": len(changes),
        "net_count_delta": net_count_delta,
        "changes": changes,
        "summary": trend_summary(changes, net_count_delta),
    }


def previous_business_snapshot():
    if not model_exists("agentic.request.log") or not table_exists("agentic_request_log"):
        return None

    logs = request.env["agentic.request.log"].sudo().search(
        [
            ("endpoint", "=", "/agentic/v1/business_snapshot"),
            ("ok", "=", True),
            ("status_code", ">=", 200),
            ("status_code", "<", 300),
        ],
        order="create_date desc, id desc",
        limit=20,
    )
    for log in logs:
        try:
            payload = json.loads(log.response_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("erp_surface"), list):
            return {
                "id": log.id,
                "create_date": log.create_date,
                "payload": payload,
            }
    return None


def trend_summary(changes, net_count_delta):
    if not changes:
        return "No tracked ERP domain counts changed since the previous successful business snapshot."

    direction = "increased" if net_count_delta > 0 else "decreased" if net_count_delta < 0 else "changed"
    top_changes = sorted(changes, key=lambda item: abs(item["delta"]), reverse=True)[:3]
    domains = ", ".join(
        f"{item['domain']} ({item['delta']:+d})"
        for item in top_changes
    )
    return f"Tracked ERP records {direction} by {net_count_delta:+d} overall. Largest domain changes: {domains}."


def bounded_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 80
    return max(1, min(limit, MAX_LIMIT))


def bounded_days(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 7
    return max(1, min(days, 90))


def bounded_offset(value):
    try:
        offset = int(value)
    except (TypeError, ValueError):
        offset = 0
    return max(0, offset)


def jsonable(value):
    if hasattr(value, "ids"):
        return value.ids
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return str(value)


def json_response(payload, status=200):
    log_api_response(payload, status)
    return Response(
        json.dumps(payload, default=str),
        status=status,
        content_type="application/json",
    )


def json_error(code, message, status):
    return json_response({
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }, status=status)


def log_api_response(payload, status):
    if not model_exists("agentic.request.log") or not table_exists("agentic_request_log"):
        return

    try:
        path = request.httprequest.path
        body = read_json()
        error = payload.get("error") if isinstance(payload, dict) else None
        operation = path.rsplit("/", 1)[-1] if path else None
        expected = os.getenv("AGENTIC_HEADLESS_API_KEY", "").strip()
        header = request.httprequest.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip()

        with request.env.cr.savepoint():
            request.env["agentic.request.log"].sudo().create({
                "name": f"{request.httprequest.method} {path}",
                "endpoint": path,
                "method": request.httprequest.method,
                "operation": operation,
                "model_name": body.get("model") if isinstance(body, dict) else None,
                "status_code": status,
                "ok": bool(payload.get("ok")) if isinstance(payload, dict) else status < 400,
                "error_code": error.get("code") if isinstance(error, dict) else None,
                "authenticated": bool(expected and token == expected),
                "remote_addr": request.httprequest.remote_addr,
                "user_agent": request.httprequest.user_agent.string,
                "payload_json": truncated_json(body),
                "response_json": truncated_json(payload),
            })
    except Exception:
        # Audit logging must never break the operational API path.
        return


def table_exists(table_name):
    with request.env.cr.savepoint():
        request.env.cr.execute("SELECT to_regclass(%s)", [table_name])
        return bool(request.env.cr.fetchone()[0])


def truncated_json(value):
    rendered = json.dumps(value, default=str, sort_keys=True)
    if len(rendered) <= MAX_LOG_JSON_CHARS:
        return rendered
    return rendered[:MAX_LOG_JSON_CHARS] + "...[truncated]"
