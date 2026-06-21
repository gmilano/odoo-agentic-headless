import json

from odoo import api, fields, models


class AgenticRequestLog(models.Model):
    _name = "agentic.request.log"
    _description = "Agentic Headless Request Log"
    _order = "create_date desc, id desc"

    name = fields.Char(required=True, default="Agentic API request")
    endpoint = fields.Char(required=True, index=True)
    method = fields.Char(required=True)
    operation = fields.Char(index=True)
    model_name = fields.Char(index=True)
    status_code = fields.Integer(required=True, index=True)
    ok = fields.Boolean(index=True)
    error_code = fields.Char(index=True)
    authenticated = fields.Boolean(index=True)
    remote_addr = fields.Char()
    user_agent = fields.Char()
    payload_json = fields.Text()
    response_json = fields.Text()


class AgenticApprovalRequest(models.Model):
    _name = "agentic.approval.request"
    _description = "Agentic Headless Approval Request"
    _order = "create_date desc, id desc"

    name = fields.Char(required=True, default="Agentic approval request")
    approval_reference = fields.Char(readonly=True, copy=False, index=True)
    status = fields.Selection(
        [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("consumed", "Consumed"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="pending",
        index=True,
    )
    risk = fields.Selection(
        [
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
        ],
        required=True,
        default="medium",
        index=True,
    )
    goal = fields.Char()
    requested_by = fields.Char()
    plan_json = fields.Text(required=True)
    normalized_operations_json = fields.Text(required=True)
    approval_note = fields.Text()
    rejection_reason = fields.Text()
    approved_by_id = fields.Many2one("res.users", readonly=True)
    approved_at = fields.Datetime(readonly=True)
    consumed_at = fields.Datetime(readonly=True)

    def action_open_mobile_review(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/agentic/ui/approvals?approval_reference={self.approval_reference}",
            "target": "self",
        }

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if not record.approval_reference:
                record.approval_reference = f"AHR-{record.id:06d}"
        return records

    def action_approve(self):
        self.write({
            "status": "approved",
            "approved_by_id": self.env.user.id,
            "approved_at": fields.Datetime.now(),
        })

    def action_reject(self):
        self.write({"status": "rejected"})

    def action_cancel(self):
        self.write({"status": "cancelled"})

    def action_mark_consumed(self):
        self.write({
            "status": "consumed",
            "consumed_at": fields.Datetime.now(),
        })
