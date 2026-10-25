# ELYREON Workflow Rescue for external agents

Hosted static validation of sanitized n8n workflow exports, operated by PHOENIX.
It returns structural defects, evidence, risk classifications, repair suggestions
and reproducible input hashes. It never executes workflow code or accesses your
n8n instance. See https://elyraon.com.co/agents/ for current access and limits.

Install the Agent Skill with the open-source Skills CLI:

```sh
npx skills add colt2822/Drex-Dotz --skill elyreon-workflow-check
```

Codex, Claude Code and OpenCode can use the same SKILL.md. Select your agent in
the installer, or copy the skill folder to your agent's supported skill directory.
Installing this package does not enroll your agent into the ELYREON fleet.

MCP clients connect to `https://elyraon.com.co/agents/mcp` using Streamable HTTP
(MCP 2025-11-25) and the free bearer token described in the public docs. The
Official MCP Registry name is `co.com.elyraon/workflow-rescue`.
Use `tools/list` and call `check_n8n_workflow` with `{"workflow": EXPORT}`.

Other integrations use the public OpenAPI at `/agents/openapi.json`, or the
A2A 0.3 JSON-RPC interface advertised by `/.well-known/agent-card.json`.
Current ARD metadata is at `/.well-known/ard.json`; the legacy
`/.well-known/ai-catalog.json` path is also served for compatible clients.

The trial is free, with 20 calls/client/day plus shared quotas, a 64 KiB request
limit and a 200-node limit. Never upload secrets or information you cannot share.
The browser-only version at `/tools/workflow-rescue/` processes exports locally.

`structural_pass` describes confirmed structural defects in the export, not runtime
correctness. Findings with `POTENTIAL_RISK` or `INSUFFICIENT_EVIDENCE` need further
investigation. A successful API call can correctly return a failed structural check.

Hosted-service telemetry retains opaque client IDs, declared sources, outcomes and
timing for 30 days. It does not retain workflow contents or reports. Public identity
and source declarations are unverified. Controlled installs and test calls are not
customers. No payment collection is enabled. The x402 module is a gated preparation
artifact and is not imported by the free production Worker.

The public Worker package is in `dotz/phoenix/commerce`. Its analyzer is extracted
unchanged from ELYREON's existing Workflow Rescue; `vendor/provenance.json` records
the source and hash. Internal PHOENIX routing and CRM integration are maintained in
the operating checkout, rather than publishing unrelated operational history.
