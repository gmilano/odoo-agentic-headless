import json

from odoo import fields, models


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
