# Distribution decisions — 2026-10-25

The target is voluntary outside adoption of the two existing services. All acceptance runs in this change are controlled. Listings, stars, traffic and successful test calls do not establish customers. No outside identity or payment is attested by this work.

## Channels and supported installation

| Ecosystem / channel | Actual path | Evidence of active ecosystem / discovery | Measurable signal and limitation | Decision |
| --- | --- | --- | --- | --- |
| Codex | Agent Skill or remote MCP config with bearer token environment variable | [Official skills](https://developers.openai.com/codex/skills), [MCP](https://developers.openai.com/codex/mcp) | Local install is observable to installer; our service observes authenticated connection and receipt, not agent identity | Publish skill + config; prioritize generated payload preflight |
| Claude Code | `claude mcp add --transport http ... --header ...`, project `.mcp.json`, skills | [Official client docs](https://code.claude.com/docs/en/mcp) | Connection and repeat token events; no unsolicited edits to anyone's assignments | Publish instructions; no Claude execution or assignments changed |
| OpenCode | `mcp` config, remote type, headers, OAuth false for bearer-token service | [Official MCP docs](https://opencode.ai/docs/mcp-servers) | Tool invocation receipts; native client installation not remotely observable | Publish config |
| Cursor | `.cursor/mcp.json`; supported MCP install links; skills | [Official install links](https://prod.cursor.com/docs/mcp/install-links) | GUI acceptance UNKNOWN without owner-controlled client; no token embedded in public deeplink | Publish config, retain private token setup |
| Hermes | `mcp_servers` in `~/.hermes/config.yaml`; HTTP URL/headers; skill hub | [Official Hermes MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) | Per-token invocation; approved catalog review distinct from arbitrary config install | Publish config; prepare catalog contribution, no unsolicited PR |
| n8n agents | MCP Client Tool or HTTP Request subworkflow called by an existing agent | [Official MCP entry](https://n8n.io/mcp/), [Creator Hub](https://support.n8n.io/article/becoming-an-n8n-template-creator-and-getting-help) | Creator approvals/usage are stronger distribution evidence than a JSON download; native fixture execution separately recorded | Publish two reusable subworkflows; creator login and review remain |
| LangChain/LangGraph | `langchain[mcp]>=1.4.0`, `MCPAdapter(fastmcp.Client(...,auth=TOKEN))` → tools passed to existing graph | [Current official docs](https://docs.langchain.com/oss/python/langchain/mcp), maintainer issue 40072 | Native tool invocation tested without paid inference; old MCP adapter repository archived | Publish tested example; do not target archived adapter repo |
| CrewAI | Streamable HTTP via MCPServerAdapter | [Official CrewAI transport](https://docs.crewai.com/en/mcp/streamable-http) | Native CrewAI execution UNKNOWN; generic MCP transport is tested | Document supported path; no untested package advertised |
| OpenAI-compatible frameworks | Existing HTTP custom tool or framework-native MCP support | Model API compatibility alone does not supply MCP; SDK example works independently of model vendor | Authenticated receipts, client identity UNKNOWN | Publish SDK/HTTP path; no new model agent |
| VS Code / other MCP clients | Input-secret remote MCP config | Existing public configuration; protocol acceptance tested | No desktop GUI install claim | Publish reusable input config |
| Official MCP Registry | Existing authenticated domain namespace, versioned server.json | [Registry API](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md) | Live search/readback is listing evidence; no installation analytics | Updated existing names, no duplicate product/alias listing |
| skills.sh / skills CLI | Public GitHub skill folder + `npx skills add URL --skill NAME` | [CLI discovery and leaderboard docs](https://www.skills.sh/docs) | Anonymous installation telemetry cannot verify identity; controlled installs disable telemetry | Publish installable branches; third-party indexing unverified |
| Smithery | Remote URL publish + scan; authenticated servers require OAuth | [Publishing requirements](https://smithery.ai/docs/build/publish) | Gateway analytics available after accepted publication | Prepared metadata; existing bearer trial is incompatible with required OAuth flow; no auth overhaul |
| Glama | Submit repository/connector through supported Add Server interface; curation | [Directory API and data sources](https://glama.ai/mcp/reference) | Approved connectors and usage metrics; API requires key | Prepared submission; account access and curation required |

Ecosystem documentation and public issue activity establish active maintenance, not that their users want our service. We have no measured channel conversion rate or customer lifetime value.

## Findability measured

`evidence/registry-before.json` records all eight requested exact phrases, plus working control queries. None of the eight found ELYREON. `workflow` found Workflow Rescue; `payload-validator` found Payload Validator. Official Registry search is a case-insensitive substring of the canonical name, not a semantic description/keyword index. No supported keyword or category fields were invented. Existing names were retained; metadata versions improve accurate descriptions but cannot make name-only search match `n8n`. That remains a registry limitation.

The owned central catalog now includes two services and accurate supported terms. Its actual browser search is tested separately. `workflow repair` returns static findings and repair suggestions, with the no-execution boundary visible. It does not advertise autonomous repair. Metadata and catalog reach are not acquisition.

## Exact publication handoffs

1. n8n Creator Hub: sign in at https://creators.n8n.io, submit the published `n8n-workflow.json` with the quickstart, then await review before submitting the second payload template. No creator account was created or review bypassed. The public GitHub/site downloads are already usable.
2. Hermes approved optional-MCP catalog: prepare the two URL/header entries from `hermes.yaml`; obtain maintainer contribution permission and follow catalog review. Do not post a promotional issue or unsolicited PR.
3. Smithery: submit the existing URL only after a supported authentication path is approved. Current services require bearer trial tokens, while Smithery's URL publication requires OAuth for authenticated services. This assignment does not rebuild authentication.
4. Glama: submit the service repository URLs and public server manifests using Add Server; retain approved connector readback before claiming a listing. Required account session/API key and curation were not available.
5. skills.sh: public skills are installable from the published branch URLs. Catalog indexing and organic install counts remain UNKNOWN; no synthetic installation telemetry was used to boost rankings.

## Existing PHOENIX acquisition cycle

The existing commerce cycle calls `acquisition.run`, using the same controller timer, existing five live instances, CRM `phoenix_opportunity`, buyer ownership, event and key/value records. Every six hours it reads at most three public issue search feeds, at most five records each, with three-second request timeouts. Failed sources back off for six hours. No sender, inference request, scheduler, financial ledger or new seat is created.

Each candidate carries its URL, demand excerpt, engagement if available, specific service fit and limits, supported integration method, concrete example URL, estimated 1–3 hours effort (planning estimate), expected monetary value UNKNOWN, funding unverified, owner and next action. `RESEARCH_ONLY` prevents treating an issue as a qualified sales lead. Existing ownership claims prevent duplicate pursuit. No outside issue text becomes executable instructions.

GUERRILLA reviews discovery/install friction; BROKER reviews framework integration packets; OPPORTUNIST compares demand and limits; CLOSER reviews verified usage and opt-in inquiries; BOUNTY HUNTER verifies funding and eligibility. These handoffs supplement their existing work without changing the 24-hour lifecycle.

## Demand, costs and pricing

[Public demand excerpts](evidence/public-demand.json) include n8n validation false positives and concurrency failures, and LangChain structured error preservation. They support reliable validation examples and clearer limits. They do not show a request to buy ELYREON. Existing validators are also substitutes. ELYREON's static check does not repair those upstream bugs or validate all node runtime semantics.

| Capability | Current capability / observed buyer evidence | Price decision |
| --- | --- | --- |
| Static workflow validation | Existing free service; public demand for reliable checks, no ELYREON buyer | Keep current free quota |
| Advanced workflow diagnostics | Requests concern runtime/node semantics beyond our static coverage | UNKNOWN willingness to pay; do not build or sell unimplemented diagnosis |
| JSON payload validation | Existing bounded deterministic service; structured result interoperability is relevant | Keep free quota; no evidence for charging commodity validation |
| Compatibility checking | Existing bounded schema checks can be used as input preflight, not full compatibility certification | Validate a named user's contract before quoting |
| Closely related integration support | Useful published examples; no accepted paid request | Quote only an independently requested, defined scope using existing commercial approval |

No inference calls are used by these services: model-provider inference cost is $0 for validation. Hosting usage is not a bill. Hosting cost, account allocation and net revenue are UNKNOWN pending provider billing evidence. Payment collection is disabled and verified service revenue is $0. The earlier $0.01/call is an unvalidated hypothesis. A price floor requires billed costs per successful external call plus support/failure allocation and an agreed margin; a viable price also requires a buyer's explicit budget/acceptance. No fabricated cost-based price is offered.

The next commercial action is to use the tested public example in a specific, permission-supported maintainer/developer conversation, observe a voluntary outside invocation and repeat, then ask what failure or integration support they would pay to remove through existing authorized channels. Public issues alone are neither consent to outreach nor paid demand.
