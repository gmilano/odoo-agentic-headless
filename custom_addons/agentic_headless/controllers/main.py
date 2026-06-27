import json
import os
from datetime import timedelta
from html import escape
from urllib.parse import quote

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
        "name": "business_cockpit",
        "endpoint": "/agentic/v1/business_cockpit",
        "method": "GET|POST",
        "risk": "low",
        "effect": "read_executive_business_metrics",
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
        "name": "execute_plan",
        "endpoint": "/agentic/v1/execute_plan",
        "method": "POST",
        "risk": "high",
        "effect": "execute_approved_action_plan_operations",
        "executes": True,
        "requires_approval": True,
    },
    {
        "name": "risk_classification",
        "endpoint": "/agentic/v1/risk_classification",
        "method": "POST",
        "risk": "low",
        "effect": "classify_destructive_or_financial_operations_without_execution",
        "executes": False,
    },
    {
        "name": "approval_requests",
        "endpoint": "/agentic/v1/approval_requests",
        "method": "GET|POST",
        "risk": "medium",
        "effect": "create_or_list_durable_agent_action_approval_requests",
        "executes": False,
        "requires_approval": False,
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
        "name": "permission_profiles",
        "endpoint": "/agentic/v1/permission_profiles",
        "method": "GET|POST",
        "risk": "low",
        "effect": "describe_role_based_permission_profiles_and_active_profile",
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
        "current_guardrail": "Private methods are blocked. Durable approval requests exist; call execution remains blocked from execute_plan.",
    },
    {
        "name": "write_financial_records",
        "reason": "Financial records can affect invoices, journals, tax reports, and external accounting state.",
        "current_guardrail": "risk_classification flags account.move create/write as high risk and execute_plan blocks account.move writes outright.",
    },
    {
        "name": "confirm_or_cancel_documents",
        "reason": "State-changing workflow methods can commit sales, purchase, stock, manufacturing, or accounting actions.",
        "current_guardrail": "Treat state transitions as approval-required and create an approval request before an adapter executes them.",
    },
]

# Risk classification policy (AH-0105). Centralizes how destructive or
# financial operations are scored before any execution path runs.
FINANCIAL_MODELS = {
    "account.move",
    "account.move.line",
    "account.payment",
    "account.payment.register",
    "account.bank.statement",
    "account.bank.statement.line",
    "account.journal",
    "account.account",
    "account.tax",
    "account.reconcile.model",
    "account.full.reconcile",
    "account.partial.reconcile",
}
DESTRUCTIVE_OPERATIONS = {"unlink", "delete", "remove"}
ARBITRARY_OPERATIONS = {"call"}
WRITE_OPERATIONS = {"create", "write", "update"}
READ_ONLY_OPERATIONS = {
    "search_read",
    "read",
    "search",
    "search_count",
    "name_search",
    "schema",
    "fields_get",
    # Comprehension endpoints surface aggregated business context without
    # mutating any record, so action plans built from them stay low risk.
    "business_snapshot",
    "business_events",
    "business_cockpit",
    "capabilities",
}
STATE_SENSITIVE_FIELDS = {
    "state",
    "active",
    "payment_state",
    "move_type",
    "amount_total",
    "amount_residual",
    "reconciled",
    "posted",
    "company_id",
    "currency_id",
}
RISK_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}

# Permission profiles (AH-0104). Defense in depth on top of the bearer token:
# even a valid API key is scoped to one role so an agent can only run the
# operations its role permits. The active profile is selected per request via
# the `X-Agentic-Profile` header, or globally via AGENTIC_HEADLESS_PROFILE.
# When neither is set the API stays fully open as `admin` for backward compat.
READ_PLAN_OPERATION_NAMES = {
    "health",
    "schema",
    "search_read",
    "business_snapshot",
    "business_events",
    "business_cockpit",
    "capabilities",
    "action_plan",
    "risk_classification",
    "okf_bundle",
    "audit_logs",
    "approval_requests",
    "permission_profiles",
}
PERMISSION_PROFILES = {
    "executive": {
        "label": "Executive (read-only)",
        "description": "Business comprehension, planning, audit review, and approval requests. Cannot write, call methods, or execute plans.",
        "operations": set(READ_PLAN_OPERATION_NAMES),
        "allow_financial": False,
    },
    "ops": {
        "label": "Operations operator",
        "description": "Everything an executive can do plus create/write/execute_plan on non-financial models. Cannot touch accounting models or invoke arbitrary methods.",
        "operations": READ_PLAN_OPERATION_NAMES | {"create", "write", "execute_plan"},
        "allow_financial": False,
    },
    "finance": {
        "label": "Finance operator",
        "description": "Everything an ops operator can do plus create/write/execute_plan on financial/accounting models. Cannot invoke arbitrary methods.",
        "operations": READ_PLAN_OPERATION_NAMES | {"create", "write", "execute_plan"},
        "allow_financial": True,
    },
    "admin": {
        "label": "Administrator",
        "description": "Unrestricted access to every agentic operation including arbitrary model method calls.",
        "operations": "*",
        "allow_financial": True,
    },
}
DEFAULT_PROFILE = "admin"

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
        "/agentic/ui/approvals",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def mobile_approvals(self, **_kwargs):
        approval_reference = required_string(request.params, "approval_reference")
        notice = required_string(request.params, "notice")
        error = required_string(request.params, "error")
        domain = []
        if approval_reference:
            domain.append(("approval_reference", "=", approval_reference))
        records = request.env["agentic.approval.request"].sudo().search(
            domain,
            order="create_date desc, id desc",
            limit=20,
        )
        return html_response(render_mobile_approvals(records, notice=notice, error=error))

    @http.route(
        "/agentic/ui/approvals/demo",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def mobile_create_demo_approval(self, **_kwargs):
        if not approval_queue_available():
            return redirect_mobile_approvals(error="Approval queue is not installed yet.")

        operation = demo_write_operation()
        normalized, validation_error = normalize_execution_operations([operation])
        if validation_error:
            return redirect_mobile_approvals(error="Demo operation could not be normalized.")

        goal = "Escalate Glob.ai demo customer follow-up"
        plan = {
            "title": goal,
            "operations": [operation],
            "demo_story": "Agent proposes a customer-risk write. Odoo requires operator approval before execution.",
        }
        record = request.env["agentic.approval.request"].sudo().create({
            "name": goal,
            "goal": goal,
            "requested_by": request.env.user.display_name,
            "risk": infer_operations_risk(normalized),
            "plan_json": json.dumps(plan, indent=2, default=str),
            "normalized_operations_json": json.dumps(normalized, indent=2, default=str),
            "approval_note": "Mobile demo request generated from /agentic/ui/approvals.",
        })
        return redirect_mobile_approvals(
            approval_reference=record.approval_reference,
            notice=f"Created {record.approval_reference}. Review and approve it before execution.",
        )

    @http.route(
        "/agentic/ui/approvals/<int:approval_id>/approve",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def mobile_approve_approval(self, approval_id, **_kwargs):
        record = request.env["agentic.approval.request"].sudo().browse(approval_id).exists()
        if not record:
            return redirect_mobile_approvals(error="Approval request not found.")
        if record.status != "pending":
            return redirect_mobile_approvals(approval_reference=record.approval_reference, error=f"{record.approval_reference} is {record.status}.")
        record.action_approve()
        return redirect_mobile_approvals(approval_reference=record.approval_reference, notice=f"Approved {record.approval_reference}.")

    @http.route(
        "/agentic/ui/approvals/<int:approval_id>/reject",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def mobile_reject_approval(self, approval_id, **_kwargs):
        record = request.env["agentic.approval.request"].sudo().browse(approval_id).exists()
        if not record:
            return redirect_mobile_approvals(error="Approval request not found.")
        if record.status != "pending":
            return redirect_mobile_approvals(approval_reference=record.approval_reference, error=f"{record.approval_reference} is {record.status}.")
        reason = required_string(request.params, "rejection_reason") or "Rejected from mobile review UI."
        record.write({"rejection_reason": reason})
        record.action_reject()
        return redirect_mobile_approvals(approval_reference=record.approval_reference, notice=f"Rejected {record.approval_reference}.")

    @http.route(
        "/agentic/ui/approvals/<int:approval_id>/execute",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def mobile_execute_approval(self, approval_id, **_kwargs):
        record = request.env["agentic.approval.request"].sudo().browse(approval_id).exists()
        if not record:
            return redirect_mobile_approvals(error="Approval request not found.")
        if record.status != "approved":
            return redirect_mobile_approvals(approval_reference=record.approval_reference, error=f"{record.approval_reference} must be approved before execution.")

        operations = parse_logged_json(record.normalized_operations_json)
        if not isinstance(operations, list) or not operations:
            return redirect_mobile_approvals(approval_reference=record.approval_reference, error="Stored approval operations are invalid.")

        validation = validate_approval_reference(record.approval_reference, operations)
        if isinstance(validation, Response):
            return redirect_mobile_approvals(approval_reference=record.approval_reference, error="Approval validation failed.")

        try:
            with request.env.cr.savepoint():
                results = [execute_operation(operation) for operation in operations]
                record.action_mark_consumed()
                log_ui_execution(record, results)
        except ExecutionPlanError as exc:
            return redirect_mobile_approvals(approval_reference=record.approval_reference, error=f"{exc.code}: {exc.message}")

        return redirect_mobile_approvals(approval_reference=record.approval_reference, notice=f"Executed {record.approval_reference}. Audit and rollback details are now available.")

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
        "/agentic/v1/business_cockpit",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        cors="*",
    )
    def business_cockpit(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        limit = min(bounded_limit(payload.get("limit", 20)), 50)
        currency = company_currency_name()
        cockpit = {
            "revenue": cockpit_revenue(limit, currency),
            "pipeline": cockpit_pipeline(limit, currency),
            "cash": cockpit_cash(limit, currency),
            "inventory_risk": cockpit_inventory_risk(limit),
            "delivery_risk": cockpit_delivery_risk(limit),
            "approvals": cockpit_approvals(limit),
        }
        return json_response({
            "ok": True,
            "database": getattr(request.env.cr, "dbname", None),
            "company": company_snapshot(),
            "as_of": odoo_fields.Datetime.to_string(odoo_fields.Datetime.now()),
            "limit": limit,
            "cockpit": cockpit,
            "insights": business_cockpit_insights(cockpit),
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
                "approved_plan_execution": True,
                "approval_queue": approval_queue_available(),
                "risk_classifier": True,
                "permission_profiles": True,
            },
            "permission_profiles": {
                "active_profile": active_profile_name(),
                "default_profile": DEFAULT_PROFILE,
                "available": sorted(PERMISSION_PROFILES),
                "selection_header": "X-Agentic-Profile",
                "endpoint": "/agentic/v1/permission_profiles",
            },
            "next_safety_gaps": [
                "Promote execute_plan rollback payloads into approval requests for reviewed reversal workflows.",
                "Bind permission profiles to distinct API keys instead of a request header so callers cannot self-escalate.",
            ],
        })

    @http.route(
        "/agentic/v1/permission_profiles",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        cors="*",
    )
    def permission_profiles(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        name, profile, error = resolve_active_profile()
        if error:
            return error

        return json_response({
            "ok": True,
            "active_profile": {
                "name": name,
                "label": profile["label"],
                "description": profile["description"],
                "allowed_operations": "*" if profile["operations"] == "*" else sorted(profile["operations"]),
                "allow_financial": profile["allow_financial"],
            },
            "default_profile": DEFAULT_PROFILE,
            "selection": {
                "header": "X-Agentic-Profile",
                "env": "AGENTIC_HEADLESS_PROFILE",
                "precedence": "request header overrides env var; unset falls back to the default profile.",
            },
            "profiles": profile_directory(),
        })

    @http.route(
        "/agentic/v1/risk_classification",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def risk_classification(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        operations = plan.get("operations") if isinstance(plan, dict) else None
        if not operations:
            operations = payload.get("operations")
        if isinstance(payload.get("operation"), str):
            operations = [payload]
        if not isinstance(operations, list) or not operations:
            return json_error(
                "invalid_classification_request",
                "Expected a non-empty operations list, plan.operations, or a single operation object.",
                400,
            )
        if len(operations) > 50:
            return json_error("too_many_operations", "risk_classification accepts at most 50 operations.", 400)

        classification = classify_operations_risk(operations)
        return json_response({
            "ok": True,
            "executes": False,
            "classification": classification,
            "policy": {
                "financial_models": sorted(FINANCIAL_MODELS),
                "destructive_operations": sorted(DESTRUCTIVE_OPERATIONS),
                "state_sensitive_fields": sorted(STATE_SENSITIVE_FIELDS),
                "approval_required_levels": ["medium", "high"],
            },
            "next_step": (
                "Create an approval request via /agentic/v1/approval_requests for medium/high risk before execute_plan."
                if classification["requires_approval"]
                else "Low risk: execute_plan may run this without a durable approval reference."
            ),
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
        "/agentic/v1/approval_requests",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        cors="*",
    )
    def approval_requests(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        if not approval_queue_available():
            return json_error("approval_queue_unavailable", "The agentic.approval.request model is not installed yet.", 503)

        payload = read_json()
        action = required_string(payload, "action")
        if not action:
            action = "create" if isinstance(payload.get("plan"), dict) or isinstance(payload.get("operations"), list) else "list"

        approval_model = request.env["agentic.approval.request"].sudo()
        if action == "create":
            plan, normalized, validation_error = approval_request_plan_and_ops(payload)
            if validation_error:
                return validation_error

            risk = required_string(payload, "risk") or infer_operations_risk(normalized)
            if risk not in {"low", "medium", "high"}:
                risk = infer_operations_risk(normalized)
            goal = required_string(payload, "goal") or plan.get("title") or plan.get("intent") or "Agentic action approval"
            record = approval_model.create({
                "name": f"Approval: {goal[:80]}",
                "goal": goal,
                "risk": risk,
                "requested_by": required_string(payload, "requested_by") or "agentic-api",
                "plan_json": json.dumps(plan, default=str, sort_keys=True),
                "normalized_operations_json": json.dumps(normalized, default=str, sort_keys=True),
                "approval_note": required_string(payload, "approval_note"),
            })
            return json_response({
                "ok": True,
                "approval_request": serialize_approval_request(record, include_plan=True),
                "execution_contract": {
                    "execute_endpoint": "/agentic/v1/execute_plan",
                    "approval_reference": record.approval_reference,
                    "next_step": "An Odoo operator must approve this request before execute_plan can consume the AHR reference.",
                },
            }, status=201)

        if action != "list":
            return json_error("unsupported_approval_action", "approval_requests supports action=create or action=list.", 400)

        limit = min(bounded_limit(payload.get("limit", 50)), 100)
        offset = bounded_offset(payload.get("offset"))
        domain = approval_request_domain(payload)
        records = approval_model.search(domain, order="create_date desc, id desc", limit=limit, offset=offset)
        return json_response({
            "ok": True,
            "database": getattr(request.env.cr, "dbname", None),
            "filters": {
                "status": required_string(payload, "status") or None,
                "risk": required_string(payload, "risk") or None,
                "approval_reference": required_string(payload, "approval_reference") or None,
                "limit": limit,
                "offset": offset,
            },
            "count": len(records),
            "total_matching": approval_model.search_count(domain),
            "approval_requests": [serialize_approval_request(record) for record in records],
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
        classification = classify_operations_risk(template.get("operations") or [])
        risk = template.get("risk") or classification["risk"]
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
                "risk_classification": {
                    "risk": classification["risk"],
                    "financial": classification["financial"],
                    "destructive": classification["destructive"],
                    "requires_approval": classification["requires_approval"],
                    "requires_durable_approval_reference": classification["requires_durable_approval_reference"],
                    "summary": classification["summary"],
                },
                "risk_factors": classification["factors"],
                "execution_contract": {
                    "execute_endpoint": "/agentic/v1/execute_plan",
                    "available": True,
                    "approval_queue_endpoint": "/agentic/v1/approval_requests",
                    "current_next_step": "Review, replace payload_template placeholders with concrete payload values, create an approval request, then call execute_plan with approved=true and the approved AHR reference.",
                },
            },
            "guardrails": [
                "This endpoint never creates, writes, confirms, posts, cancels, or calls model methods.",
                "Financial, inventory workflow, and arbitrary method actions remain approval-required; use approval_requests for durable review.",
                "Use search_read/schema/capabilities to resolve IDs and validate fields before execution.",
            ],
        })

    @http.route(
        "/agentic/v1/execute_plan",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def execute_plan(self, **_kwargs):
        auth_error = require_api_key()
        if auth_error:
            return auth_error

        payload = read_json()
        approved = optional_bool(payload.get("approved"))
        if approved is not True:
            return json_error("approval_required", "Expected approved=true before executing a plan.", 403)

        approval_reference = required_string(payload, "approval_reference")
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
        operations = plan.get("operations") if isinstance(plan, dict) else None
        if not isinstance(operations, list) or not operations:
            return json_error("invalid_plan", "Expected a non-empty operations list.", 400)
        if len(operations) > 10:
            return json_error("too_many_operations", "execute_plan accepts at most 10 operations per request.", 400)

        normalized, validation_error = normalize_execution_operations(operations)
        if validation_error:
            return validation_error

        classification = classify_operations_risk(normalized)
        profile_error = enforce_profile("execute_plan", financial=classification["financial"])
        if profile_error:
            return profile_error

        if requires_durable_approval_reference(normalized) and not is_durable_approval_reference(approval_reference):
            return json_error(
                "durable_approval_required",
                "Medium/high risk create/write operations require an approved AHR approval_reference from /agentic/v1/approval_requests.",
                403,
            )

        approval_request = None
        if is_durable_approval_reference(approval_reference):
            approval_request = validate_approval_reference(approval_reference, normalized)
            if isinstance(approval_request, Response):
                return approval_request

        results = []
        try:
            with request.env.cr.savepoint():
                for operation in normalized:
                    results.append(execute_operation(operation))
                if approval_request:
                    approval_request.action_mark_consumed()
        except ExecutionPlanError as error:
            return json_error(error.code, error.message, error.status)

        return json_response({
            "ok": True,
            "approved": True,
            "approval_reference": approval_reference or None,
            "executed_count": len(results),
            "results": results,
            "audit": {
                "request_log": "This execute_plan request and response are captured in agentic.request.log.",
                "rollback_scope": "Operations run inside one database savepoint; execution errors roll back this plan batch.",
                "approval_request": serialize_approval_request(approval_request) if approval_request else None,
            },
            "rollback_hints": rollback_hints(normalized, results),
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
        model_name = required_string(payload, "model")
        profile_error = enforce_profile("create", financial=model_name in FINANCIAL_MODELS)
        if profile_error:
            return profile_error
        model = get_model(model_name)
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
        model_name = required_string(payload, "model")
        profile_error = enforce_profile("write", financial=model_name in FINANCIAL_MODELS)
        if profile_error:
            return profile_error
        model = get_model(model_name)
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

        profile_error = enforce_profile("call")
        if profile_error:
            return profile_error

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


def resolve_active_profile():
    """Resolve the permission profile for this request.

    Returns ``(name, profile_dict, error_response)``. ``error_response`` is a
    JSON 403 when an unknown profile is requested; otherwise it is ``None``.
    """
    name = (
        request.httprequest.headers.get("X-Agentic-Profile")
        or os.getenv("AGENTIC_HEADLESS_PROFILE")
        or DEFAULT_PROFILE
    ).strip().lower()
    profile = PERMISSION_PROFILES.get(name)
    if not profile:
        known = ", ".join(sorted(PERMISSION_PROFILES))
        return name, None, json_error(
            "unknown_profile",
            f"Unknown permission profile '{name}'. Known profiles: {known}.",
            403,
        )
    return name, profile, None


def enforce_profile(operation_name, financial=False):
    """Authorize an operation against the active permission profile.

    Returns a JSON 403 ``Response`` when the active profile may not run the
    operation (or touch financial models), otherwise ``None``.
    """
    name, profile, error = resolve_active_profile()
    if error:
        return error
    operations = profile["operations"]
    if operations != "*" and operation_name not in operations:
        return json_error(
            "operation_not_permitted_for_profile",
            f"Permission profile '{name}' ({profile['label']}) may not use operation '{operation_name}'.",
            403,
        )
    if financial and not profile["allow_financial"]:
        return json_error(
            "financial_not_permitted_for_profile",
            f"Permission profile '{name}' ({profile['label']}) may not operate on financial/accounting models.",
            403,
        )
    return None


def profile_directory():
    """Serialize all permission profiles for discovery responses."""
    directory = []
    for name, profile in PERMISSION_PROFILES.items():
        operations = profile["operations"]
        directory.append({
            "name": name,
            "label": profile["label"],
            "description": profile["description"],
            "allowed_operations": "*" if operations == "*" else sorted(operations),
            "allow_financial": profile["allow_financial"],
        })
    return directory


def active_profile_name():
    return (
        request.httprequest.headers.get("X-Agentic-Profile")
        or os.getenv("AGENTIC_HEADLESS_PROFILE")
        or DEFAULT_PROFILE
    ).strip().lower()


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


def company_currency_name():
    if not model_exists("res.company"):
        return None
    company = request.env.company.sudo()
    return company.currency_id.name if company.currency_id else None


def cockpit_model_unavailable(model_name):
    return {
        "available": False,
        "model": model_name,
        "summary": "Model is not installed in this Odoo database.",
    }


def cockpit_model(model_name):
    if not model_exists(model_name):
        return None
    return request.env[model_name].sudo().with_context(agentic_headless=True)


def cockpit_fields(model):
    return set(model._fields)


def cockpit_domain(model, clauses):
    fields = cockpit_fields(model)
    return [clause for clause in clauses if not clause or clause[0] in fields]


def cockpit_search_read(model, domain, fields, limit, order=None):
    available = [field for field in fields if field in model._fields]
    if "display_name" not in available:
        available.append("display_name")
    order = cockpit_order(model, order)
    return model.search_read(
        domain=domain,
        fields=available,
        limit=limit,
        order=order,
    )


def cockpit_order(model, order):
    if order:
        first = str(order).split(",", 1)[0].strip().split(" ", 1)[0]
        if first in model._fields:
            return order
    return default_order(model)


def cockpit_sum(model, domain, field_name, limit=500):
    if field_name not in model._fields:
        return None
    rows = model.search_read(domain=domain, fields=[field_name], limit=limit)
    return sum(float(row.get(field_name) or 0) for row in rows)


def cockpit_count(model, domain):
    return model.search_count(domain)


def cockpit_revenue(limit, currency):
    model = cockpit_model("sale.order")
    if model is None:
        return cockpit_model_unavailable("sale.order")

    confirmed_domain = cockpit_domain(model, [("state", "in", ["sale", "done"])])
    quotation_domain = cockpit_domain(model, [("state", "in", ["draft", "sent"])])
    fields = ["name", "partner_id", "state", "amount_total", "date_order", "invoice_status"]
    return {
        "available": True,
        "model": "sale.order",
        "currency": currency,
        "confirmed_total": cockpit_sum(model, confirmed_domain, "amount_total"),
        "confirmed_count": cockpit_count(model, confirmed_domain),
        "quotation_total": cockpit_sum(model, quotation_domain, "amount_total"),
        "quotation_count": cockpit_count(model, quotation_domain),
        "top_confirmed_orders": cockpit_search_read(model, confirmed_domain, fields, limit, order="amount_total desc"),
    }


def cockpit_pipeline(limit, currency):
    model = cockpit_model("crm.lead")
    if model is None:
        return cockpit_model_unavailable("crm.lead")

    domain = cockpit_domain(model, [("active", "=", True)])
    rows = cockpit_search_read(
        model,
        domain,
        ["name", "partner_id", "stage_id", "expected_revenue", "probability", "priority", "date_deadline"],
        limit,
        order="expected_revenue desc",
    )
    total = sum(float(row.get("expected_revenue") or 0) for row in rows)
    weighted = sum(
        float(row.get("expected_revenue") or 0) * float(row.get("probability") or 0) / 100
        for row in rows
    )
    return {
        "available": True,
        "model": "crm.lead",
        "currency": currency,
        "open_count": cockpit_count(model, domain),
        "sample_total_expected_revenue": total,
        "sample_weighted_expected_revenue": weighted,
        "top_opportunities": rows,
        "note": "Pipeline totals are based on the returned sample limit until a read_group aggregation is added.",
    }


def cockpit_cash(limit, currency):
    model = cockpit_model("account.move")
    if model is None:
        return cockpit_model_unavailable("account.move")

    receivable_domain = cockpit_domain(
        model,
        [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ["not_paid", "partial"]),
        ],
    )
    payable_domain = cockpit_domain(
        model,
        [
            ("move_type", "=", "in_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ["not_paid", "partial"]),
        ],
    )
    amount_field = "amount_residual" if "amount_residual" in model._fields else "amount_total"
    fields = ["name", "partner_id", "move_type", "state", "payment_state", amount_field, "invoice_date_due"]
    return {
        "available": True,
        "model": "account.move",
        "currency": currency,
        "open_receivables_total": cockpit_sum(model, receivable_domain, amount_field),
        "open_receivables_count": cockpit_count(model, receivable_domain),
        "open_payables_total": cockpit_sum(model, payable_domain, amount_field),
        "open_payables_count": cockpit_count(model, payable_domain),
        "largest_open_receivables": cockpit_search_read(model, receivable_domain, fields, limit, order=f"{amount_field} desc"),
        "largest_open_payables": cockpit_search_read(model, payable_domain, fields, limit, order=f"{amount_field} desc"),
    }


def cockpit_inventory_risk(limit):
    model = cockpit_model("stock.quant")
    if model is None:
        return cockpit_model_unavailable("stock.quant")

    negative_domain = cockpit_domain(model, [("quantity", "<", 0)])
    reserved_domain = cockpit_domain(model, [("reserved_quantity", ">", 0)])
    fields = ["product_id", "location_id", "quantity", "reserved_quantity", "available_quantity"]
    return {
        "available": True,
        "model": "stock.quant",
        "negative_stock_count": cockpit_count(model, negative_domain),
        "reserved_stock_count": cockpit_count(model, reserved_domain),
        "negative_stock": cockpit_search_read(model, negative_domain, fields, limit, order="quantity asc"),
        "reserved_stock": cockpit_search_read(model, reserved_domain, fields, limit, order="reserved_quantity desc"),
    }


def cockpit_delivery_risk(limit):
    model = cockpit_model("stock.picking")
    if model is None:
        return cockpit_model_unavailable("stock.picking")

    now = odoo_fields.Datetime.now()
    domain = cockpit_domain(
        model,
        [
            ("state", "not in", ["done", "cancel"]),
            ("scheduled_date", "<", now),
            ("picking_type_code", "=", "outgoing"),
        ],
    )
    fields = ["name", "partner_id", "state", "scheduled_date", "picking_type_id", "origin"]
    return {
        "available": True,
        "model": "stock.picking",
        "overdue_outgoing_count": cockpit_count(model, domain),
        "overdue_outgoing": cockpit_search_read(model, domain, fields, limit, order="scheduled_date asc"),
    }


def cockpit_approvals(limit):
    model = cockpit_model("agentic.approval.request")
    if model is None:
        return cockpit_model_unavailable("agentic.approval.request")

    pending_domain = [("status", "=", "pending")]
    high_domain = [("status", "=", "pending"), ("risk", "=", "high")]
    fields = ["approval_reference", "status", "risk", "goal", "requested_by", "create_date"]
    return {
        "available": True,
        "model": "agentic.approval.request",
        "pending_count": cockpit_count(model, pending_domain),
        "pending_high_risk_count": cockpit_count(model, high_domain),
        "pending": cockpit_search_read(model, pending_domain, fields, limit, order="create_date desc"),
    }


def business_cockpit_insights(cockpit):
    insights = []
    revenue = cockpit.get("revenue") or {}
    pipeline = cockpit.get("pipeline") or {}
    cash = cockpit.get("cash") or {}
    inventory = cockpit.get("inventory_risk") or {}
    delivery = cockpit.get("delivery_risk") or {}
    approvals = cockpit.get("approvals") or {}

    if revenue.get("available") and not revenue.get("confirmed_count"):
        insights.append({
            "level": "commercial",
            "title": "No confirmed revenue found",
            "detail": "The cockpit sees quotations or CRM context, but no confirmed sale orders yet.",
        })
    if pipeline.get("available") and pipeline.get("open_count"):
        insights.append({
            "level": "pipeline",
            "title": "Open opportunities are ready for agent review",
            "detail": f"{pipeline.get('open_count')} CRM opportunities can be prioritized by expected and weighted revenue.",
        })
    if cash.get("available") and (cash.get("open_receivables_count") or cash.get("open_payables_count")):
        insights.append({
            "level": "cash",
            "title": "Cash exposure is visible",
            "detail": f"Open receivables: {cash.get('open_receivables_count')}; open payables: {cash.get('open_payables_count')}.",
        })
    if inventory.get("available") and inventory.get("negative_stock_count"):
        insights.append({
            "level": "inventory",
            "title": "Negative stock needs operational review",
            "detail": f"{inventory.get('negative_stock_count')} stock quants are below zero.",
        })
    if delivery.get("available") and delivery.get("overdue_outgoing_count"):
        insights.append({
            "level": "delivery",
            "title": "Overdue outgoing deliveries detected",
            "detail": f"{delivery.get('overdue_outgoing_count')} outgoing pickings are scheduled in the past and not done.",
        })
    if approvals.get("available") and approvals.get("pending_count"):
        insights.append({
            "level": "approval",
            "title": "Agent actions are waiting for human review",
            "detail": f"{approvals.get('pending_count')} pending approval requests, {approvals.get('pending_high_risk_count')} high risk.",
        })
    if not insights:
        insights.append({
            "level": "ready",
            "title": "Executive cockpit is available",
            "detail": "No immediate revenue, cash, inventory, delivery, or approval risks were detected from the tracked models.",
        })
    return insights


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


def render_mobile_approvals(records, notice=None, error=None):
    cards = "\n".join(render_mobile_approval_card(record) for record in records)
    if not cards:
        cards = """
        <section class="empty">
            <h2>No approval requests yet</h2>
            <p>Create a demo request to show the full agentic ERP review flow.</p>
        </section>
        """

    notice_html = f'<div class="notice">{escape(notice)}</div>' if notice else ""
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Agentic ERP Approvals</title>
    <style>
        :root {{
            color-scheme: light;
            --bg: #f7f8fb;
            --ink: #172033;
            --muted: #667085;
            --line: #d9dee8;
            --card: #ffffff;
            --accent: #155eef;
            --ok: #067647;
            --warn: #b54708;
            --bad: #b42318;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            background: var(--bg);
            color: var(--ink);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            line-height: 1.35;
        }}
        header {{
            position: sticky;
            top: 0;
            z-index: 2;
            padding: 18px 16px 14px;
            background: rgba(247, 248, 251, .94);
            border-bottom: 1px solid var(--line);
            backdrop-filter: blur(12px);
        }}
        h1 {{ margin: 0; font-size: 24px; letter-spacing: 0; }}
        h2 {{ margin: 0 0 6px; font-size: 18px; }}
        .sub {{ margin-top: 5px; color: var(--muted); font-size: 14px; }}
        main {{ max-width: 760px; margin: 0 auto; padding: 16px; }}
        .actions {{
            display: grid;
            gap: 10px;
            margin: 0 0 14px;
        }}
        .card, .empty {{
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 8px;
            padding: 14px;
            margin-bottom: 12px;
            box-shadow: 0 1px 2px rgba(16, 24, 40, .04);
        }}
        .topline {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 8px;
        }}
        .ref {{ font-size: 18px; font-weight: 700; }}
        .goal {{ font-size: 15px; margin: 4px 0 0; color: var(--muted); }}
        .chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; }}
        .chip {{
            display: inline-flex;
            align-items: center;
            min-height: 26px;
            padding: 3px 8px;
            border-radius: 999px;
            border: 1px solid var(--line);
            font-size: 12px;
            color: var(--muted);
            background: #f9fafb;
        }}
        .chip.pending {{ color: var(--warn); background: #fffaeb; border-color: #fedf89; }}
        .chip.approved {{ color: var(--ok); background: #ecfdf3; border-color: #abefc6; }}
        .chip.rejected, .chip.cancelled {{ color: var(--bad); background: #fef3f2; border-color: #fecdca; }}
        .chip.consumed {{ color: #344054; background: #eef4ff; border-color: #c7d7fe; }}
        pre {{
            white-space: pre-wrap;
            overflow-wrap: anywhere;
            background: #101828;
            color: #f2f4f7;
            border-radius: 8px;
            padding: 12px;
            font-size: 12px;
            line-height: 1.4;
            max-height: 260px;
            overflow: auto;
        }}
        button {{
            width: 100%;
            min-height: 44px;
            border: 0;
            border-radius: 8px;
            padding: 10px 12px;
            font-size: 15px;
            font-weight: 700;
            background: var(--accent);
            color: white;
        }}
        .secondary {{ background: #344054; }}
        .danger {{ background: var(--bad); }}
        .ghost {{
            background: white;
            color: var(--ink);
            border: 1px solid var(--line);
        }}
        form {{ margin: 0; }}
        .buttonrow {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
            margin-top: 10px;
        }}
        .notice, .error {{
            border-radius: 8px;
            padding: 10px 12px;
            margin-bottom: 12px;
            font-size: 14px;
            border: 1px solid;
        }}
        .notice {{ background: #ecfdf3; border-color: #abefc6; color: var(--ok); }}
        .error {{ background: #fef3f2; border-color: #fecdca; color: var(--bad); }}
        .meta {{ color: var(--muted); font-size: 12px; margin-top: 8px; }}
        @media (min-width: 620px) {{
            .actions {{ grid-template-columns: 1fr 1fr; }}
            .buttonrow {{ grid-template-columns: repeat(3, 1fr); }}
        }}
    </style>
</head>
<body>
    <header>
        <h1>Agentic ERP</h1>
        <div class="sub">Human approval queue for risky Odoo agent actions</div>
    </header>
    <main>
        {notice_html}
        {error_html}
        <section class="actions">
            <form method="post" action="/agentic/ui/approvals/demo">
                <button type="submit">Create demo approval</button>
            </form>
            <form method="get" action="/web">
                <button class="ghost" type="submit">Open Odoo backend</button>
            </form>
        </section>
        {cards}
    </main>
</body>
</html>"""


def render_mobile_approval_card(record):
    plan = escape(record.plan_json or "{}")
    status = escape(record.status or "")
    risk = escape(record.risk or "")
    ref = escape(record.approval_reference or "")
    goal = escape(record.goal or record.name or "")
    requested_by = escape(record.requested_by or "agent")
    created = escape(str(record.create_date or ""))
    approved_by = escape(record.approved_by_id.display_name if record.approved_by_id else "")
    buttons = render_mobile_approval_buttons(record)
    approved_by_html = f'<span class="chip">approved by {approved_by}</span>' if approved_by else ""
    return f"""
    <section class="card">
        <div class="topline">
            <div>
                <div class="ref">{ref}</div>
                <p class="goal">{goal}</p>
            </div>
            <span class="chip {status}">{status}</span>
        </div>
        <div class="chips">
            <span class="chip">risk: {risk}</span>
            <span class="chip">requested by {requested_by}</span>
            {approved_by_html}
        </div>
        <pre>{plan}</pre>
        {buttons}
        <div class="meta">Created {created}. Execution consumes the approval and records rollback hints in the audit trail.</div>
    </section>
    """


def render_mobile_approval_buttons(record):
    if record.status == "pending":
        return f"""
        <div class="buttonrow">
            <form method="post" action="/agentic/ui/approvals/{record.id}/approve">
                <button type="submit">Approve</button>
            </form>
            <form method="post" action="/agentic/ui/approvals/{record.id}/reject">
                <button class="danger" type="submit">Reject</button>
            </form>
            <form method="get" action="/web#id={record.id}&model=agentic.approval.request&view_type=form">
                <button class="ghost" type="submit">Backend</button>
            </form>
        </div>
        """
    if record.status == "approved":
        return f"""
        <div class="buttonrow">
            <form method="post" action="/agentic/ui/approvals/{record.id}/execute">
                <button class="secondary" type="submit">Execute approved plan</button>
            </form>
            <form method="get" action="/web#id={record.id}&model=agentic.approval.request&view_type=form">
                <button class="ghost" type="submit">Backend</button>
            </form>
        </div>
        """
    return f"""
    <div class="buttonrow">
        <form method="get" action="/web#id={record.id}&model=agentic.approval.request&view_type=form">
            <button class="ghost" type="submit">Backend</button>
        </form>
    </div>
    """


def html_response(html, status=200):
    return Response(html, status=status, content_type="text/html; charset=utf-8")


def redirect_mobile_approvals(approval_reference=None, notice=None, error=None):
    params = []
    if approval_reference:
        params.append(f"approval_reference={quote(str(approval_reference))}")
    if notice:
        params.append(f"notice={quote(str(notice))}")
    if error:
        params.append(f"error={quote(str(error))}")
    suffix = "?" + "&".join(params) if params else ""
    return request.redirect(f"/agentic/ui/approvals{suffix}")


def log_ui_execution(record, results):
    if not model_exists("agentic.request.log") or not table_exists("agentic_request_log"):
        return
    operations = parse_logged_json(record.normalized_operations_json)
    request.env["agentic.request.log"].sudo().create({
        "name": f"UI execute {record.approval_reference}",
        "endpoint": "/agentic/ui/approvals/execute",
        "method": "POST",
        "operation": "execute_plan",
        "model_name": "agentic.approval.request",
        "status_code": 200,
        "ok": True,
        "authenticated": True,
        "remote_addr": request.httprequest.remote_addr,
        "user_agent": request.httprequest.user_agent.string,
        "payload_json": truncated_json({
            "approval_reference": record.approval_reference,
            "operations": operations,
        }),
        "response_json": truncated_json({
            "ok": True,
            "approval_reference": record.approval_reference,
            "executed_count": len(results),
            "results": results,
            "rollback_hints": rollback_hints(operations, results),
        }),
    })


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


def approval_queue_available():
    return model_exists("agentic.approval.request") and table_exists("agentic_approval_request")


def approval_request_plan_and_ops(payload):
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    operations = plan.get("operations") if isinstance(plan, dict) else None
    if not operations:
        operations = payload.get("operations")
    if not isinstance(operations, list) or not operations:
        return None, None, json_error("invalid_approval_plan", "Expected a non-empty plan.operations or operations list.", 400)
    if len(operations) > 10:
        return None, None, json_error("too_many_operations", "Approval requests accept at most 10 operations.", 400)

    normalized, validation_error = normalize_execution_operations(operations)
    if validation_error:
        return None, None, validation_error

    if not plan:
        plan = {
            "title": required_string(payload, "goal") or "Agentic action approval",
            "operations": operations,
        }
    return plan, normalized, None


def approval_request_domain(payload):
    domain = []
    status = required_string(payload, "status")
    risk = required_string(payload, "risk")
    approval_reference = required_string(payload, "approval_reference")
    if status:
        domain.append(("status", "=", status))
    if risk:
        domain.append(("risk", "=", risk))
    if approval_reference:
        domain.append(("approval_reference", "=", approval_reference))
    since_days = payload.get("since_days")
    if since_days is not None:
        since = odoo_fields.Datetime.now() - timedelta(days=bounded_days(since_days))
        domain.append(("create_date", ">=", odoo_fields.Datetime.to_string(since)))
    return domain


def serialize_approval_request(record, include_plan=False):
    if not record:
        return None
    item = {
        "id": record.id,
        "approval_reference": record.approval_reference,
        "status": record.status,
        "risk": record.risk,
        "goal": record.goal,
        "requested_by": record.requested_by,
        "created_at": record.create_date,
        "approved_by": record.approved_by_id.display_name if record.approved_by_id else None,
        "approved_at": record.approved_at,
        "consumed_at": record.consumed_at,
    }
    if include_plan:
        item.update({
            "plan": parse_logged_json(record.plan_json),
            "normalized_operations": parse_logged_json(record.normalized_operations_json),
            "approval_note": record.approval_note,
            "rejection_reason": record.rejection_reason,
        })
    return item


def infer_operations_risk(operations):
    return classify_operations_risk(operations)["risk"]


def classify_operations_risk(operations):
    """Classify a list of proposed operations for destructive/financial risk.

    Works on both normalized execute operations and raw action-plan operation
    templates. Does not execute anything; it only scores intent so callers can
    require approval, block, or surface the risk to a human or agent.
    """
    factors = [classify_one_operation(operation, index) for index, operation in enumerate(operations or [])]
    overall = "low"
    for factor in factors:
        overall = higher_risk(overall, factor["risk"])
    financial = any(factor["financial"] for factor in factors)
    destructive = any(factor["destructive"] for factor in factors)
    requires_approval = overall in {"medium", "high"}
    return {
        "risk": overall,
        "financial": financial,
        "destructive": destructive,
        "requires_approval": requires_approval,
        "requires_durable_approval_reference": requires_approval,
        "operation_count": len(factors),
        "factors": factors,
        "summary": risk_classification_summary(overall, financial, destructive, len(factors)),
    }


def classify_one_operation(operation, index):
    if not isinstance(operation, dict):
        return {
            "index": index,
            "operation": None,
            "model": None,
            "risk": "high",
            "financial": False,
            "destructive": False,
            "reasons": ["Operation is not a structured object and cannot be safely classified."],
        }

    name = (required_string(operation, "operation") or "").lower()
    payload = operation_payload(operation) or {}
    model = (required_string(operation, "model") or required_string(payload, "model") or "").strip()
    values = operation_values(operation, payload)
    financial = bool(model) and model in FINANCIAL_MODELS
    destructive = name in DESTRUCTIVE_OPERATIONS
    reasons = []
    risk = "low"

    if destructive:
        risk = "high"
        reasons.append(f"Destructive '{name}' permanently removes records.")
    elif name in ARBITRARY_OPERATIONS:
        risk = "high"
        reasons.append("Arbitrary model method call can trigger workflows outside typed guardrails.")
    elif name in WRITE_OPERATIONS:
        risk = "medium"
        reasons.append(f"'{name}' changes persisted business records.")
        if financial:
            risk = "high"
            reasons.append(f"Targets financial model '{model}' affecting accounting state.")
        sensitive = sorted(STATE_SENSITIVE_FIELDS & set(values.keys()))
        if sensitive:
            risk = "high"
            reasons.append(f"Modifies state-sensitive field(s): {', '.join(sensitive)}.")
    elif name in READ_ONLY_OPERATIONS or not name:
        reasons.append("Read-only or metadata operation with no state change.")
    else:
        risk = "medium"
        reasons.append(f"Unrecognized operation '{name}' treated as medium risk by default.")

    if financial and risk != "high":
        reasons.append(f"Operates on financial model '{model}'.")

    return {
        "index": operation.get("index", index),
        "operation": name or None,
        "model": model or None,
        "risk": risk,
        "financial": financial,
        "destructive": destructive,
        "reasons": reasons,
    }


def operation_values(operation, payload):
    for source in (payload, operation):
        if not isinstance(source, dict):
            continue
        for key in ("values", "vals", "data"):
            candidate = source.get(key)
            if isinstance(candidate, dict):
                return candidate
    return {}


def higher_risk(current, candidate):
    if RISK_LEVEL_ORDER.get(candidate, 0) > RISK_LEVEL_ORDER.get(current, 0):
        return candidate
    return current


def risk_classification_summary(overall, financial, destructive, count):
    if count == 0:
        return "No operations supplied; nothing to classify."
    tags = []
    if destructive:
        tags.append("destructive")
    if financial:
        tags.append("financial")
    detail = f" ({', '.join(tags)})" if tags else ""
    plural = "operation" if count == 1 else "operations"
    return f"{count} {plural} classified as {overall} risk{detail}."


def requires_durable_approval_reference(operations):
    return infer_operations_risk(operations) in {"medium", "high"}


def is_durable_approval_reference(value):
    return bool(value and isinstance(value, str) and value.startswith("AHR-"))


def validate_approval_reference(approval_reference, normalized_operations):
    if not approval_queue_available():
        return json_error("approval_queue_unavailable", "The approval queue is not installed; use an external approval reference.", 503)

    record = request.env["agentic.approval.request"].sudo().search(
        [("approval_reference", "=", approval_reference)],
        limit=1,
    )
    if not record:
        return json_error("approval_reference_unknown", f"Unknown approval reference: {approval_reference}", 404)
    if record.status != "approved":
        return json_error(
            "approval_reference_not_approved",
            f"Approval reference {approval_reference} is {record.status}, not approved.",
            403,
        )

    approved_operations = parse_logged_json(record.normalized_operations_json)
    if comparable_operations(approved_operations) != comparable_operations(normalized_operations):
        return json_error(
            "approval_reference_plan_mismatch",
            "The approved operations do not match the execute_plan operations.",
            403,
        )
    return record


def comparable_operations(operations):
    return json.dumps(operations, default=str, sort_keys=True)


def demo_write_operation():
    if model_exists("crm.lead"):
        lead = demo_crm_lead()
        values = {}
        if "probability" in lead._fields:
            values["probability"] = 88
        if "priority" in lead._fields:
            values["priority"] = "3"
        if not values and "description" in lead._fields:
            values["description"] = "Agentic ERP demo: escalated from mobile approval UI."
        if values:
            return {
                "operation": "write",
                "purpose": "Escalate a strategic CRM opportunity after agent review.",
                "payload": {
                    "model": "crm.lead",
                    "ids": [lead.id],
                    "values": values,
                },
            }

    partner = demo_partner()
    field_name = demo_partner_write_field()
    return {
        "operation": "write",
        "purpose": "Mark a strategic customer for proactive executive follow-up.",
        "payload": {
            "model": "res.partner",
            "ids": [partner.id],
            "values": {
                field_name: demo_partner_field_value(field_name),
            },
        },
    }


def demo_crm_lead():
    partner = demo_partner()
    lead_model = request.env["crm.lead"].sudo()
    lead = lead_model.search([("name", "=", "Glob.ai Renewal - South Region")], limit=1)
    if lead:
        return lead
    values = {
        "name": "Glob.ai Renewal - South Region",
        "partner_id": partner.id,
        "expected_revenue": 185000,
        "probability": 62,
    }
    if "type" in lead_model._fields:
        values["type"] = "opportunity"
    if "priority" in lead_model._fields:
        values["priority"] = "2"
    return lead_model.create(values)


def demo_partner():
    partner_model = request.env["res.partner"].sudo()
    partner = partner_model.search([("name", "=", "Glob.ai Demo Customer")], limit=1)
    if partner:
        return partner
    return partner_model.create({
        "name": "Glob.ai Demo Customer",
        "email": "buyer@globai-demo.example",
        "phone": "+598 2900 0000",
        "website": "https://glob.ai",
    })


def demo_partner_write_field():
    fields = request.env["res.partner"]._fields
    for field_name in ["comment", "website", "phone", "email"]:
        if field_name in fields:
            return field_name
    return "name"


def demo_partner_field_value(field_name):
    if field_name == "comment":
        return "Agentic ERP demo: executive follow-up approved from mobile review UI."
    if field_name == "website":
        return "https://glob.ai/demo-approved"
    if field_name == "phone":
        return "+598 2900 0001"
    if field_name == "email":
        return "approved-buyer@globai-demo.example"
    return "Glob.ai Demo Customer - Approved"


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


class ExecutionPlanError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def normalize_execution_operations(operations):
    normalized = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            return None, json_error("invalid_operation", f"Operation {index} must be an object.", 400)

        operation_name = required_string(operation, "operation")
        if operation_name not in {"search_read", "create", "write"}:
            return None, json_error(
                "unsupported_operation",
                f"Operation {index} uses unsupported operation '{operation_name}'. execute_plan currently supports search_read, create, and write.",
                400,
            )

        payload = operation_payload(operation)
        if not isinstance(payload, dict):
            return None, json_error("invalid_operation_payload", f"Operation {index} must include an object payload.", 400)
        if contains_placeholder(payload):
            return None, json_error(
                "unresolved_placeholder",
                f"Operation {index} still contains '<...>' placeholders. Replace templates with concrete values before execution.",
                400,
            )

        model_name = required_string(payload, "model") or required_string(operation, "model")
        if not model_name:
            return None, json_error("missing_model", f"Operation {index} is missing a model.", 400)
        if not model_exists(model_name):
            return None, json_error("unknown_model", f"Operation {index} references unknown model: {model_name}", 404)
        if operation_name in {"create", "write"} and model_name == "account.move":
            return None, json_error(
                "financial_write_blocked",
                "execute_plan blocks account.move writes until the approval queue and financial risk classifier exist.",
                403,
            )

        normalized.append({
            "index": index,
            "operation": operation_name,
            "model": model_name,
            "payload": payload,
            "purpose": operation.get("purpose"),
        })
    return normalized, None


def operation_payload(operation):
    payload = operation.get("payload")
    if isinstance(payload, dict):
        return payload
    payload_template = operation.get("payload_template")
    if isinstance(payload_template, dict):
        return payload_template
    return None


def contains_placeholder(value):
    if isinstance(value, str):
        return "<" in value and ">" in value
    if isinstance(value, list):
        return any(contains_placeholder(item) for item in value)
    if isinstance(value, tuple):
        return any(contains_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(contains_placeholder(item) for item in value.values())
    return False


def execute_operation(operation):
    model = request.env[operation["model"]].sudo().with_context(agentic_headless=True)
    payload = operation["payload"]
    operation_name = operation["operation"]

    if operation_name == "search_read":
        rows = model.search_read(
            domain=payload.get("domain") or [],
            fields=payload.get("fields"),
            offset=bounded_offset(payload.get("offset")),
            limit=bounded_limit(payload.get("limit", 80)),
            order=payload.get("order"),
        )
        return {
            "index": operation["index"],
            "operation": operation_name,
            "model": operation["model"],
            "ok": True,
            "count": len(rows),
            "rows": rows,
        }

    if operation_name == "create":
        values = payload.get("values")
        if not isinstance(values, dict):
            raise ExecutionPlanError("invalid_values", f"Operation {operation['index']} expected object field 'values'.")
        record = model.create(values)
        return {
            "index": operation["index"],
            "operation": operation_name,
            "model": operation["model"],
            "ok": True,
            "id": record.id,
            "display_name": record.display_name,
        }

    if operation_name == "write":
        ids = payload.get("ids")
        values = payload.get("values")
        if not isinstance(ids, list) or not all(isinstance(item, int) for item in ids):
            raise ExecutionPlanError("invalid_ids", f"Operation {operation['index']} expected integer list field 'ids'.")
        if not isinstance(values, dict):
            raise ExecutionPlanError("invalid_values", f"Operation {operation['index']} expected object field 'values'.")
        records = model.browse(ids).exists()
        previous_values = previous_field_values(records, values)
        records.write(values)
        return {
            "index": operation["index"],
            "operation": operation_name,
            "model": operation["model"],
            "ok": True,
            "updated": len(records),
            "ids": records.ids,
            "previous_values": previous_values,
            "rollback_plan": rollback_plan_for_previous_values(operation["model"], previous_values),
        }

    raise ExecutionPlanError("unsupported_operation", f"Unsupported operation: {operation_name}")


def rollback_hints(operations, results):
    hints = []
    for operation, result in zip(operations, results):
        if operation["operation"] == "create":
            hints.append({
                "operation_index": operation["index"],
                "created_model": operation["model"],
                "created_id": result.get("id"),
                "hint": "Review and archive/delete the created record if the approved action was wrong.",
            })
        elif operation["operation"] == "write":
            hints.append({
                "operation_index": operation["index"],
                "updated_model": operation["model"],
                "updated_ids": result.get("ids", []),
                "previous_values": result.get("previous_values", []),
                "rollback_plan": result.get("rollback_plan"),
                "hint": "Review the captured previous values, then execute the rollback plan only after a fresh approval.",
            })
    if not hints:
        hints.append({
            "operation_index": None,
            "hint": "Read-only operations do not require rollback.",
        })
    return hints


def previous_field_values(records, values):
    if not records or not isinstance(values, dict):
        return []

    field_names = [field for field in values if field in records._fields]
    if not field_names:
        return []

    return [
        {
            "id": item["id"],
            "values": {
                field: jsonable(item.get(field))
                for field in field_names
            },
        }
        for item in records.read(field_names)
    ]


def rollback_plan_for_previous_values(model_name, previous_values):
    return {
        "operations": [
            {
                "operation": "write",
                "model": model_name,
                "payload": {
                    "model": model_name,
                    "ids": [item["id"]],
                    "values": item["values"],
                },
            }
            for item in previous_values
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
