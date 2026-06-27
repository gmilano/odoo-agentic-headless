/**
 * Odoo Agentic Headless — Cloudflare Workers MCP Server
 *
 * Implements MCP over HTTP+SSE (Streamable HTTP transport).
 * GEAI connects via: { "mcpServers": { "odoo": { "uri": "https://<worker>.workers.dev/sse" } } }
 *
 * Cloudflare agentic readiness checklist:
 *  ✅ Stateless — no Node.js APIs, pure Web Platform (fetch/Response/Headers)
 *  ✅ HTTP+SSE MCP transport (GET /sse + POST /message)
 *  ✅ Streamable HTTP: POST /mcp (bidirectional, single endpoint)
 *  ✅ CORS headers for browser/GEAI proxy access
 *  ✅ Auth via Bearer token (forwarded to Odoo)
 *  ✅ Per-sector Odoo permission profile via X-Agentic-Profile
 *
 * Environment variables (set via wrangler.toml or Workers dashboard):
 *   ODOO_BASE_URL         — e.g. https://odoo.yourdomain.com
 *   ODOO_AGENTIC_API_KEY  — bearer token for Odoo headless API
 *   MCP_AUTH_TOKEN        — bearer token callers must present (optional)
 *   DEFAULT_PROFILE       — executive | ops | finance | admin (default: admin)
 */

const TOOLS = [
  {
    name: "odoo_business_snapshot",
    description:
      "Real-time business snapshot: record counts across CRM, Sales, Inventory, Accounting, Projects. Includes trend memory vs previous snapshot.",
    inputSchema: {
      type: "object",
      properties: {
        sample_limit: { type: "integer", default: 5, description: "Max sample records per domain" },
      },
    },
  },
  {
    name: "odoo_business_events",
    description:
      "Recent business events (creates/writes) across all ERP domains. Returns normalized events with actor metadata and signals.",
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "integer", default: 10 },
        hours: { type: "integer", default: 48, description: "Lookback window in hours" },
      },
    },
  },
  {
    name: "odoo_business_cockpit",
    description:
      "Executive cockpit: revenue, CRM pipeline, cash exposure (receivables/payables), inventory risk, delivery risk, pending approvals.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "odoo_action_plan",
    description:
      "Convert a natural-language business goal into a typed Odoo operation plan WITHOUT executing. Returns risk classification and approval requirements.",
    inputSchema: {
      type: "object",
      required: ["goal"],
      properties: {
        goal: { type: "string", description: "Business goal in natural language" },
        candidate_values: { type: "object", description: "Pre-filled values for plan placeholders" },
      },
    },
  },
  {
    name: "odoo_execute_plan",
    description:
      "Execute an approved action plan. Requires approved=true. Medium/high risk plans need a durable approval_reference (AHR-...).",
    inputSchema: {
      type: "object",
      required: ["plan", "approved"],
      properties: {
        plan: { type: "object" },
        approved: { type: "boolean" },
        approval_reference: { type: "string", description: "AHR-... reference for risky plans" },
      },
    },
  },
  {
    name: "odoo_approval_requests",
    description:
      "Create or list durable approval requests for risky operations. Queue for human review before execution.",
    inputSchema: {
      type: "object",
      properties: {
        action: { type: "string", enum: ["list", "create"], default: "list" },
        goal: { type: "string" },
        plan: { type: "object" },
        risk: { type: "string", enum: ["low", "medium", "high"] },
        requested_by: { type: "string" },
        status_filter: { type: "string", enum: ["pending", "approved", "rejected", "consumed", "all"], default: "pending" },
      },
    },
  },
  {
    name: "odoo_audit_logs",
    description: "Query the agentic audit log. Filter by endpoint, operation, model, status, or time window.",
    inputSchema: {
      type: "object",
      properties: {
        endpoint: { type: "string" },
        operation: { type: "string" },
        status: { type: "string", enum: ["ok", "error", "unauthorized"] },
        recent_hours: { type: "integer", default: 24 },
        limit: { type: "integer", default: 20 },
        include_payloads: { type: "boolean", default: false },
      },
    },
  },
  {
    name: "odoo_risk_classification",
    description: "Classify risk of planned Odoo operations before executing. Returns risk level, financial/destructive flags, per-operation factors.",
    inputSchema: {
      type: "object",
      required: ["operations"],
      properties: {
        operations: {
          type: "array",
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
    description: "List available permission profiles (executive/ops/finance/admin) and the active profile for this session.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "odoo_capabilities",
    description: "Discover Odoo capabilities: installed modules, tracked models, allowed/risky operations, guardrails.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "odoo_okf_bundle",
    description: "Export Odoo business context as Open Knowledge Format (OKF) Markdown. Returns a structured knowledge bundle.",
    inputSchema: { type: "object", properties: {} },
  },
];

// ── Odoo proxy ────────────────────────────────────────────────────────────────

async function callOdoo(env, path, body = {}, profile = null) {
  const url = `${env.ODOO_BASE_URL}${path}`;
  const profile_ = profile || env.DEFAULT_PROFILE || "admin";
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.ODOO_AGENTIC_API_KEY}`,
      "X-Agentic-Profile": profile_,
      "X-Caller": "cloudflare-worker-mcp",
    },
    body: JSON.stringify(body),
  });
  return res.json();
}

async function dispatchTool(name, args, env, profile) {
  switch (name) {
    case "odoo_business_snapshot":
      return callOdoo(env, "/agentic/v1/business_snapshot", args, profile);
    case "odoo_business_events":
      return callOdoo(env, "/agentic/v1/business_events", args, profile);
    case "odoo_business_cockpit":
      return callOdoo(env, "/agentic/v1/business_cockpit", {}, profile);
    case "odoo_action_plan":
      return callOdoo(env, "/agentic/v1/action_plan", args, profile);
    case "odoo_execute_plan":
      return callOdoo(env, "/agentic/v1/execute_plan", args, profile);
    case "odoo_approval_requests": {
      const { action = "list", status_filter = "pending", ...rest } = args;
      return callOdoo(env, "/agentic/v1/approval_requests",
        action === "list" ? { status: status_filter } : rest, profile);
    }
    case "odoo_audit_logs":
      return callOdoo(env, "/agentic/v1/audit_logs", args, profile);
    case "odoo_risk_classification":
      return callOdoo(env, "/agentic/v1/risk_classification", args, profile);
    case "odoo_permission_profiles":
      return callOdoo(env, "/agentic/v1/permission_profiles", {}, profile);
    case "odoo_capabilities":
      return callOdoo(env, "/agentic/v1/capabilities", {}, profile);
    case "odoo_okf_bundle":
      return callOdoo(env, "/agentic/v1/okf_bundle", {}, profile);
    default:
      return { error: `Unknown tool: ${name}` };
  }
}

// ── MCP JSON-RPC handler ──────────────────────────────────────────────────────

async function handleMcpMessage(msg, env, profile) {
  let req;
  try {
    req = typeof msg === "string" ? JSON.parse(msg) : msg;
  } catch {
    return { jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } };
  }

  const { id, method, params = {} } = req;

  if (method === "initialize") {
    return {
      jsonrpc: "2.0", id,
      result: {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: {
          name: "odoo-agentic-headless",
          version: "19.0.1.14.0",
          description: "Odoo Agentic Headless MCP — Cloudflare Workers",
        },
      },
    };
  }

  if (method === "tools/list") {
    return { jsonrpc: "2.0", id, result: { tools: TOOLS } };
  }

  if (method === "tools/call") {
    const { name, arguments: args = {} } = params;
    try {
      const result = await dispatchTool(name, args, env, profile);
      return {
        jsonrpc: "2.0", id,
        result: { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] },
      };
    } catch (err) {
      return {
        jsonrpc: "2.0", id,
        result: {
          content: [{ type: "text", text: JSON.stringify({ error: err.message }) }],
          isError: true,
        },
      };
    }
  }

  if (method === "notifications/initialized") {
    return null;
  }

  return { jsonrpc: "2.0", id, error: { code: -32601, message: `Method not found: ${method}` } };
}

// ── Auth ──────────────────────────────────────────────────────────────────────

function checkAuth(request, env) {
  if (!env.MCP_AUTH_TOKEN) return true;
  const auth = request.headers.get("Authorization") || "";
  return auth === `Bearer ${env.MCP_AUTH_TOKEN}`;
}

function cors(response) {
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  headers.set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Agentic-Profile");
  return new Response(response.body, { status: response.status, headers });
}

// ── HTTP+SSE transport ────────────────────────────────────────────────────────
//
// GET  /sse     → SSE stream (server pushes, client listens)
// POST /message → client sends MCP JSON-RPC, response via SSE
// POST /mcp     → Streamable HTTP: single endpoint, bidirectional
// GET  /health  → health check

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const profile = request.headers.get("X-Agentic-Profile") || env.DEFAULT_PROFILE || "admin";

    if (request.method === "OPTIONS") {
      return cors(new Response(null, { status: 204 }));
    }

    if (!checkAuth(request, env)) {
      return cors(new Response(JSON.stringify({ error: "unauthorized" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }));
    }

    // Health check
    if (url.pathname === "/health") {
      return cors(new Response(JSON.stringify({
        ok: true,
        server: "odoo-agentic-headless-mcp",
        version: "19.0.1.14.0",
        transport: ["http+sse", "streamable-http"],
        profile,
        odoo_url: env.ODOO_BASE_URL,
      }), { headers: { "Content-Type": "application/json" } }));
    }

    // Streamable HTTP — single POST endpoint (preferred by newer MCP clients)
    if (url.pathname === "/mcp" && request.method === "POST") {
      const body = await request.json();
      const result = await handleMcpMessage(body, env, profile);
      return cors(new Response(JSON.stringify(result), {
        headers: { "Content-Type": "application/json" },
      }));
    }

    // SSE endpoint — client connects and receives server messages
    if (url.pathname === "/sse" && request.method === "GET") {
      const { readable, writable } = new TransformStream();
      const writer = writable.getWriter();
      const encoder = new TextEncoder();

      // Send server info event on connect
      const initEvent = `event: server_info\ndata: ${JSON.stringify({
        protocolVersion: "2024-11-05",
        serverInfo: { name: "odoo-agentic-headless", version: "19.0.1.14.0" },
        messageEndpoint: new URL("/message", request.url).toString(),
      })}\n\n`;
      writer.write(encoder.encode(initEvent));

      // Keepalive
      const keepalive = setInterval(() => {
        writer.write(encoder.encode(`: keepalive\n\n`)).catch(() => clearInterval(keepalive));
      }, 15000);

      request.signal.addEventListener("abort", () => {
        clearInterval(keepalive);
        writer.close();
      });

      return cors(new Response(readable, {
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      }));
    }

    // POST /message — client sends MCP message, returns JSON response
    if (url.pathname === "/message" && request.method === "POST") {
      const body = await request.json();
      const result = await handleMcpMessage(body, env, profile);
      if (!result) {
        return cors(new Response(null, { status: 204 }));
      }
      return cors(new Response(JSON.stringify(result), {
        headers: { "Content-Type": "application/json" },
      }));
    }

    return cors(new Response(JSON.stringify({ error: "Not found", paths: ["/health", "/sse", "/message", "/mcp"] }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    }));
  },
};
