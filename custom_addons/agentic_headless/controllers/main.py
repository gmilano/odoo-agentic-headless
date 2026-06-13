import json
import os

from odoo import http
from odoo.http import Response, request


MAX_LIMIT = 200
SNAPSHOT_SAMPLE_LIMIT = 3

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
            "insights": business_insights(available, modules),
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


def available_fields(model, requested_fields):
    all_fields = model.fields_get(attributes=["string", "type"])
    return [field for field in requested_fields if field in all_fields]


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


def bounded_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 80
    return max(1, min(limit, MAX_LIMIT))


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
