# Install ELYREON validation in an existing agent

Start at https://elyraon.com.co/agents/. Two free services; separate tokens, 20 calls/token/day, shared limits. Static findings are not runtime certification. Send sanitized input only. No payment or wallet.

## 1. Enroll once; reuse your token

POST JSON `{ "source":"codex" }` to `https://elyraon.com.co/agents/trial` for Workflow Rescue or `https://elyreon-payload-validator.colt2822.workers.dev/trial` for Payload Validator. Save the returned token in your client secret store. For controlled evaluation add `"test":true`; all examples tested by ELYREON use that flag. Valid sources include codex, claude_code, opencode, cursor, hermes, n8n, langchain, crewai, openai_agents, vscode, github, skill_directory, mcp_registry. Source is client-claimed.

## 2. Connect one service

- Codex: merge [codex.toml](codex.toml) into your Codex config; export the named token variable before launch. Or `codex mcp add elyreon-payload --url https://elyreon-payload-validator.colt2822.workers.dev/mcp --bearer-token-env-var ELYREON_PAYLOAD_TOKEN`.
- Claude Code: `claude mcp add --transport http elyreon-payload https://elyreon-payload-validator.colt2822.workers.dev/mcp --header "Authorization: Bearer YOUR_TOKEN"`. This config is also usable as project `.mcp.json`: [mcp.json](mcp.json).
- Cursor: merge [mcp.json](mcp.json) into `.cursor/mcp.json`; replace the private token placeholder.
- OpenCode: merge [opencode.json](opencode.json) into project config; replace the token placeholder.
- Hermes: merge [hermes.yaml](hermes.yaml) into `~/.hermes/config.yaml`; replace the token placeholder.
- VS Code: [payload MCP input configuration](vscode-payload.json) prompts for the token.
- Any Streamable HTTP MCP client: connect to the service's `/mcp` endpoint with `Authorization: Bearer TOKEN`, list tools, then call `check_n8n_workflow` with `{workflow}` or `validate_payload` with `{schema,payload}`.

Configuration transport acceptance does not establish native GUI acceptance or an outside installation.

## 3. Make the next change checkable

[check.py](check.py) is a runnable official Python MCP SDK and HTTP example. Install `httpx mcp==2.3.0`, set `ELYREON_PAYLOAD_TOKEN` or `ELYREON_WORKFLOW_TOKEN`, then run:

```sh
python check.py --service payload --input sanitized-request.json
python check.py --service workflow --input sanitized-export-wrapper.json
# Controlled demo with built-in sanitized data and two calls:
python check.py --service payload --test --repeat 2
```

For LangChain/LangGraph install `langchain[mcp]>=1.4.0` and use [langchain_check.py](langchain_check.py). It creates a native LangChain tool and invokes it without spending model tokens. Pass the discovered tools to your existing agent/graph; the old adapters repository is archived.

For n8n import [workflow validation subworkflow](n8n-workflow.json) or [payload validation subworkflow](n8n-payload.json). Enroll once with source `n8n`. Create an HTTP Header Auth credential named as shown in the HTTP Request node: name `Authorization`, value `Bearer TOKEN`. Call this subworkflow from an existing agent with the sanitized `{workflow}` or `{schema,payload}` input. The pinned input is a demonstration fixture. Check `structural_pass` or `valid` in the result before proceeding. Templates do not import or execute the submitted n8n export.

CrewAI supports remote Streamable HTTP tools using its MCPServerAdapter; OpenAI-compatible model transport alone does not add MCP. Use your framework's MCP client with the configuration above or call the HTTP endpoint from your existing custom tool. These are documented paths; no untested native framework package is advertised.

## Read the result

Workflow Rescue: `structural_pass`, `report.findings`, `report.limitations`, `receipt_id`. Payload Validator: `verdict` (PASS/FAIL/REJECTED), `valid`, `errors`, `receipt_id`. A FAIL is a completed validation identifying defective input. REJECTED means unsupported schema or limits: simplify the schema rather than assuming validation passed. 401: recheck token. 429: wait for the quota window. Recheck the corrected input using the saved token.

## Install the existing skills

```sh
npx skills add https://github.com/colt2822/Drex-Dotz/tree/codex/agent-acquisition-v2 --skill elyreon-workflow-check
npx skills add https://github.com/colt2822/Drex-Decision-Gateway-for-Coding-Agents/tree/codex/payload-acquisition-v2 --skill elyreon-payload-validator
```

Or copy each public SKILL.md into a client-supported skill folder. Controlled CLI installs disable telemetry (`DISABLE_TELEMETRY=1 DO_NOT_TRACK=1`) so they are not promoted as organic installations.
