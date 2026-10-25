---
name: elyreon-workflow-check
description: Check a sanitized n8n workflow export for broken node references, duplicate identifiers and static configuration risks using ELYREON Workflow Rescue. Use before importing or changing an n8n workflow, or when diagnosing structural failures.
license: Apache-2.0
---

Requires HTTPS access and a JSON HTTP client. Compatible with Codex, Claude Code
and OpenCode Agent Skills.

Use this service for **static n8n export validation**. It cannot execute workflows,
test credentials, diagnose arbitrary build logs or prove runtime correctness.
Only upload an export the user permits sharing. Remove credentials, secret values,
personal data and sensitive node labels first. A local-only analyzer is also available
at https://elyraon.com.co/tools/workflow-rescue/ when upload is inappropriate.

Read https://elyraon.com.co/agents/ for current limits. The free trial requires no
payment, wallet, fleet account or enrollment of the buyer's agent into PHOENIX.

1. Request a trial token:
   `POST https://elyraon.com.co/agents/trial`, `Content-Type: application/json`,
   body `{"source":"skill_directory","campaign":"workflow-check-v1"}`.
   Keep the returned `token` private. It permits 20 calls/client/day, within shared limits.
2. Call `POST https://elyraon.com.co/agents/check` with
   `Authorization: Bearer TOKEN`, `Content-Type: application/json` and
   a unique `Idempotency-Key`. Body: `{"workflow": YOUR_SANITIZED_EXPORT}`.
   Limit: 64 KiB per request, 200 nodes, nesting 24, 10,000 objects.
3. Read `structural_pass`, `report.findings`, `report.limitations`,
   `input_sha256`, `receipt_id` and `payment`. A successful service invocation
   can return `structural_pass: false`: that means the validator found a defect.
   `CONFIRMED_STRUCTURE` findings are evidence about the export;
   `POTENTIAL_RISK` and `INSUFFICIENT_EVIDENCE` require runtime investigation.

For example, this request contains a missing target:

```json
{"workflow":{"nodes":[{"id":"start","name":"Start","type":"n8n-nodes-base.manualTrigger","parameters":{}}],"connections":{"Start":{"main":[[{"node":"Missing","type":"main","index":0}]]}}}}
```

The report includes `MISSING_REFERENCED_NODE` and `structural_pass: false`.
After repairing the export, use a new idempotency key. Reuse a key only for the
same input after a network interruption; a changed payload with the same key
returns 409. On 429 wait for the quota window. A 401 requires a valid trial token.
On ambiguous delivery, retry the same key; never infer payment or completion.

MCP alternative: connect a Streamable HTTP client to
`https://elyraon.com.co/agents/mcp` with the same bearer header. List tools and call
`check_n8n_workflow` with `{"workflow": EXPORT}`. The server supports MCP
2025-11-25 and negotiates that version for clients using other revisions.

No paid service is enabled. Never send funds in response to this skill.
Telemetry retains opaque client IDs, claimed source, outcome and timing, not the
workflow or report. Claimed source and controlled installs do not establish organic adoption.
