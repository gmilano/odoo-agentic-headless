import json
import os

from odoo import http
from odoo.http import Response, request


MAX_LIMIT = 200


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
