#!/usr/bin/env node
// MCP server that exposes the Odoo Agentic Headless API as tools for GEAI agents.
// Complies with AH-0103.
//
// Usage:
//   ODOO_BASE_URL=http://localhost:8069 ODOO_AGENTIC_API_KEY=your-key node scripts/mcp-server.js
//
// GEAI registers this via:
//   { "mcpServers": { "odoo": { "command": "node", "args": ["scripts/mcp-server.js"] } } }

import { createServer } from "node:http";
import { createInterface } from "node:readline";

const ODOO_BASE_URL = process.env.ODOO_BASE_URL || "http://localhost:8069";
const API_KEY = process.env.ODOO_AGENTIC_API_KEY || "";
const AGENTIC_PROFILE = process.env.AGENTIC_HEADLESS_PROFILE || "admin";

// ── Tool definitions ──────────────────────────────────────────────────────────

const TOOLS = [
  {
    name: "odoo_business_snapshot",
    description:
      "Get a real-time snapshot of the business: record counts across CRM, Sales, Inventory, Accounting, Projects, and HR. Includes trend memory comparing to the previous snapshot.",
    inputSchema: {
      type: "object",
      properties: {
        sample_limit: {
          type: "integer",
          description: "Max sample records per domain (default 5)",
          default: 5,
        },
      },
    },
  },
  {
    name: "odoo_business_events",
    description:
      "Get recent business events (creates and writes) across CRM, Sales, Inventory, Accounting, and Projects. Returns normalized event list with actor metadata and business signals.",
    inputSchema: {
      type: "object",
      properties: {
        limit: {
          type: "integer",
          description: "Max events per domain (default 10)",
          default: 10,
        },
        hours: {
          type: "integer",
          description: "Lookback window in hours (default 48)",
          default: 48,
        },
      },
    },
  },
  {
    name: "odoo_business_cockpit",
    description:
      "Get an executive business cockpit: revenue, CRM pipeline, cash exposure (receivables/payables), inventory risk, delivery risk, and pending approval requests.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "odoo_action_plan",
    description:
      "Given a natural-language business goal, return a typed executable plan of Odoo operations WITHOUT executing them. Includes risk classification and approval requirements.",
    inputSchema: {
      type: "object",
      required: ["goal"],
      properties: {
        goal: {
          type: "string",
          description: "Business goal in natural language (e.g. 'Follow up on overdue invoices')",
        },
        candidate_values: {
          type: "object",
          description: "Optional pre-filled values for plan placeholders",
        },
      },
    },
  },
  {
    name: "odoo_execute_plan",
    description:
      "Execute a previously approved action plan. Requires approved=true and a durable approval_reference (AHR-...) for medium/high risk operations. Returns rollback hints.",
    inputSchema: {
      type: "object",
      required: ["plan", "approved"],
      properties: {
        plan: {
          type: "object",
          description: "The plan object returned by odoo_action_plan",
        },
        approved: {
          type: "boolean",
          description: "Must be true — confirms the plan has been reviewed by a human",
        },
        approval_reference: {
          type: "string",
          description: "Durable approval reference (AHR-...) required for medium/high risk plans",
        },
      },
    },
  },
  {
    name: "odoo_approval_requests",
    description:
      "Create or list durable approval requests for risky agent actions. Use this to queue operations for human review before executing.",
    inputSchema: {
      type: "object",
      properties: {
        action: {
          type: "string",
          enum: ["list", "create"],
          description: "List pending approvals or create a new request",
          default: "list",
        },
        goal: {
          type: "string",
          description: "Business goal (required for action=create)",
        },
        plan: {
          type: "object",
          description: "Plan to approve (required for action=create)",
        },
        risk: {
          type: "string",
          enum: ["low", "medium", "high"],
          description: "Risk level of the plan",
        },
        requested_by: {
          type: "string",
          description: "Agent or user requesting the approval",
        },
        status_filter: {
          type: "string",
          enum: ["pending", "approved", "rejected", "consumed", "all"],
          description: "Filter for list action",
          default: "pending",
        },
      },
    },
  },
  {
    name: "odoo_audit_logs",
    description:
      "Query the agentic audit log for API call history. Filter by endpoint, operation, model, status, or time window.",
    inputSchema: {
      type: "object",
      properties: {
        endpoint: { type: "string", description: "Filter by endpoint path" },
        operation: { type: "string", description: "Filter by operation name" },
        status: { type: "string", enum: ["ok", "error", "unauthorized"] },
        recent_hours: {
          type: "integer",
          description: "Lookback window in hours (default 24)",
          default: 24,
        },
        limit: { type: "integer", default: 20 },
        include_payloads: {
          type: "boolean",
          description: "Include request/response payloads",
          default: false,
        },
      },
    },
  },
  {
    name: "odoo_risk_classification",
    description:
      "Classify the risk level of a set of planned Odoo operations before executing them. Returns risk level, financial/destructive flags, and per-operation risk factors.",
    inputSchema: {
      type: "object",
      required: ["operations"],
      properties: {
        operations: {
          type: "array",
          description: "Array of planned operations to classify",
          items: {
            type: "object",
            properties: {
              operation: { type: "string" },
              model: { type: "string" },
              fields: { type: "object" },
            },
          },
        },
      },
    },
  },
  {
    name: "odoo_permission_profiles",
    description:
      "List available permission profiles (executive, ops, finance, admin) and the currently active profile for this session.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "odoo_capabilities",
    description:
      "Discover Odoo capabilities: installed modules, tracked ERP models, allowed/risky operations, and current safety guardrails.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "odoo_okf_bundle",
    description:
      "Export Odoo business context as Open Knowledge Format (OKF) Markdown files. Returns a structured knowledge bundle about the company's ERP state.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
];

// ── Odoo API client ───────────────────────────────────────────────────────────

async function callOdoo(path, body = {}) {
  const url = `${ODOO_BASE_URL}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${API_KEY}`,
      "X-Agentic-Profile": AGENTIC_PROFILE,
    },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    return { ok: false, error: "parse_error", raw: text };
  }
}

async function callOdooGet(path) {
  const url = `${ODOO_BASE_URL}${path}`;
  const res = await fetch(url, {
    method: "GET",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "X-Agentic-Profile": AGENTIC_PROFILE,
    },
  });
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    return { ok: false, error: "parse_error", raw: text };
  }
}

// ── Tool dispatch ─────────────────────────────────────────────────────────────

async function callTool(name, args) {
  switch (name) {
    case "odoo_business_snapshot":
      return callOdoo("/agentic/v1/business_snapshot", args);

    case "odoo_business_events":
      return callOdoo("/agentic/v1/business_events", args);

    case "odoo_business_cockpit":
      return callOdoo("/agentic/v1/business_cockpit", args);

    case "odoo_action_plan":
      return callOdoo("/agentic/v1/action_plan", args);

    case "odoo_execute_plan":
      return callOdoo("/agentic/v1/execute_plan", args);

    case "odoo_approval_requests": {
      const { action = "list", status_filter = "pending", ...rest } = args;
      if (action === "list") {
        return callOdoo("/agentic/v1/approval_requests", { status: status_filter });
      }
      return callOdoo("/agentic/v1/approval_requests", rest);
    }

    case "odoo_audit_logs":
      return callOdoo("/agentic/v1/audit_logs", args);

    case "odoo_risk_classification":
      return callOdoo("/agentic/v1/risk_classification", args);

    case "odoo_permission_profiles":
      return callOdoo("/agentic/v1/permission_profiles", {});

    case "odoo_capabilities":
      return callOdoo("/agentic/v1/capabilities", {});

    case "odoo_okf_bundle":
      return callOdoo("/agentic/v1/okf_bundle", {});

    default:
      return { error: `Unknown tool: ${name}` };
  }
}

// ── MCP JSON-RPC protocol ─────────────────────────────────────────────────────

function mcp_response(id, result) {
  return JSON.stringify({ jsonrpc: "2.0", id, result });
}

function mcp_error(id, code, message) {
  return JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } });
}

async function handleMessage(msg) {
  let req;
  try {
    req = JSON.parse(msg);
  } catch {
    return mcp_error(null, -32700, "Parse error");
  }

  const { id, method, params = {} } = req;

  if (method === "initialize") {
    return mcp_response(id, {
      protocolVersion: "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: {
        name: "odoo-agentic-headless",
        version: "19.0.1.14.0",
        description: "Odoo Agentic Headless API — GEAI-native ERP tools",
      },
    });
  }

  if (method === "notifications/initialized") {
    return null;
  }

  if (method === "tools/list") {
    return mcp_response(id, { tools: TOOLS });
  }

  if (method === "tools/call") {
    const { name, arguments: args = {} } = params;
    try {
      const result = await callTool(name, args);
      return mcp_response(id, {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      });
    } catch (err) {
      return mcp_response(id, {
        content: [{ type: "text", text: JSON.stringify({ error: err.message }) }],
        isError: true,
      });
    }
  }

  return mcp_error(id, -32601, `Method not found: ${method}`);
}

// ── Stdio transport (standard MCP) ───────────────────────────────────────────

const rl = createInterface({ input: process.stdin, terminal: false });

rl.on("line", async (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  const response = await handleMessage(trimmed);
  if (response) {
    process.stdout.write(response + "\n");
  }
});

rl.on("close", () => process.exit(0));

process.stderr.write(
  `[odoo-mcp] Odoo Agentic Headless MCP server ready. Profile: ${AGENTIC_PROFILE}\n`
);
