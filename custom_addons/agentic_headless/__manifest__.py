{
    "name": "Agentic Headless API",
    "summary": "JSON API for agentic, headless Odoo operations",
    "version": "19.0.1.11.0",
    "category": "Technical",
    "license": "LGPL-3",
    "author": "Wany",
    "depends": ["base"],
    "data": [
        "security/ir.model.access.csv",
        "views/agentic_approval_request_views.xml",
        "views/agentic_request_log_views.xml",
        "views/agentic_menus.xml",
    ],
    "installable": True,
    "application": False,
}
