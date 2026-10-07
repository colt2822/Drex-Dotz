# Drex Dotz — Reuse Matrix (Phase 1 audit)

Audit date: 2026-10-06. Sources inspected on the local machine (current source, not docs only):

| Short name | Location inspected | Owner | License | Public? |
|---|---|---|---|---|
| **FIREWALL** | `drex-agent-firewall` (public GitHub `colt2822/Drex-Agent-Firewall`, `main` @ `8233784`, v0.1.3rc1) | project author | MIT | yes |
| **HARNESS** | `drex-hermes-harness` (`harness.py`, `plugin/`, `economy/`, `native_provider/`) | project author | none declared (private, author-owned) | no |
| **SHEPHERD** | `drex-pr-shepherd-public` (`pr_shepherd/core`, `storage/sqlite.py`) | project author | Apache-2.0 | yes |
| **GATEWAY** | `drex-decision-gateway` | project author | MIT | yes |
| **DREX-MCP** | `Drex-MCP` | project author | MIT | yes |
| **HERMES** | `hermes-agent` (NousResearch, `cron/`, `plugins/`, `hermes_cli/projects_db`) | Nous Research | MIT | yes (third-party) |
| **NOVA** | `nova/router`, NOVA Responses endpoint, `nova-harness` profile plugin | private | proprietary | **no** |
| **PRIVATE** | `ELYRAON-CONTROL`, FETT, trading/copy-trader, lead engine, Money/Treasury prompts | private | proprietary | **no** |

Reuse classes: `REUSE_AS_IS` (imported unchanged as a dependency), `WRAP_EXISTING` (thin adapter around unchanged code),
`EXTRACT_GENERIC` (author-owned generic logic copied/adapted into Dotz), `SMALL_EXTENSION` (small new code on top of a reused primitive),
`NEW_CODE_REQUIRED`, `PRIVATE_DO_NOT_COPY`.

## Matrix

| FEATURE | EXISTING_SOURCE | REUSE_CLASS | NEW_CODE_ESTIMATE | LICENSE/OPEN_SOURCE_RISK | NOTES |
|---|---|---|---|---|---|
| Policy decisions (ALLOW/BLOCK/ESCALATE) | FIREWALL `policy/engine.py`, `policy/deterministic_rules.py` | REUSE_AS_IS | 0 | MIT dependency, none | Dotz never evaluates paths/commands/domains itself. |
| Policy packs per Dot role | FIREWALL `policy/packs.py` (`read-only-research`, `safe-local-coding`, `production-ops`) | REUSE_AS_IS | ~10 (select + override roots/domains) | none | `drex.pack` field in Dot config. |
| Protected reads/writes | FIREWALL `adapters/filesystem_adapter.py` | REUSE_AS_IS | 0 | none | Probed: workspace read ALLOW; `~/.ssh`, `/etc`, `../` BLOCK. |
| Workspace confinement | FIREWALL `security/path_validator.py` via `filesystem.allowed_roots` | WRAP_EXISTING | ~15 | none | Same approach as HARNESS `select_workspace`. |
| Shell/tool execution | FIREWALL `adapters/shell_adapter.py` | WRAP_EXISTING | ~30 (argv templates for tests/services) | none | Dots never pass free-form shell unless `shell.execute` is explicitly allowed. |
| Web/API reads | FIREWALL `adapters/http_adapter.py` + `network.allowed_domains` | REUSE_AS_IS | ~5 | none | |
| Secret redaction | FIREWALL `security/redactor.py` (`SecretRedactor`) | REUSE_AS_IS | 0 | none | Used for every receipt field. |
| Audit/decision records | FIREWALL `persistence/repository.py` (`audit_actions`, executions) | REUSE_AS_IS | 0 | none | Dotz receipts reference firewall `action_id`. |
| Local deterministic decision provider | HARNESS `LocalBoundaryProvider` (25 lines) | EXTRACT_GENERIC | ~25 | author-owned, relicensable | Labelled `LOCAL_BOUNDARY`; never claims a Drex prediction. |
| Live Drex provider | FIREWALL `providers/drex_provider.py`, `providers/factory.py` | REUSE_AS_IS | ~5 | none | Enabled by `DREX_API_KEY`; optional. |
| Capability-rule BLOCK recorded in audit | HARNESS `capability_decision()` pattern (`repository.record_decision`) | EXTRACT_GENERIC | ~20 | author-owned | |
| Envelope identity injection (dot/session/task) | HARNESS `select_workspace` normalizer wrap | EXTRACT_GENERIC | ~8 | author-owned | Binds firewall audit rows to `dot_id`/`task_id`. |
| Bounded grants (bind dot/task/session/workspace/files/TTL/uses) | HARNESS `_issue_mutation_grant` / `_get_mutation_grant` | EXTRACT_GENERIC + SMALL_EXTENSION | ~70 | author-owned | HARNESS keeps grants in a process dict w/ monotonic time; Dotz persists them in SQLite with wall-clock expiry so restarts cannot resurrect or lose them. |
| Receipts | FIREWALL audit + HARNESS `decision_record()` field set | SMALL_EXTENSION | ~50 | none | WHO/DOT/TASK/WHY/MODEL/CAPABILITY/GRANT/TARGET/ACTION/COST/RESULT/VERIFICATION/TS. |
| Usage/cost accounting (unknown = null, never estimated) | HARNESS `economy/accounting.py`, `native_provider/usage.py` | EXTRACT_GENERIC (convention) | ~25 | author-owned | Code is Hermes-home specific; the null-for-unknown convention and field names are reused. |
| Engineering pipeline (file authority, JSON edit contract, one repair) | HARNESS `plugin/economy.py` (`file_authority`, `decode_edits`, `pipeline`) | EXTRACT_GENERIC | ~90 | author-owned; NOVA-specific worker gate removed | Used by the optional NOVA adapter. |
| Scheduler | HERMES `cron` (`hermes cron create … --no-agent --script`) | WRAP_EXISTING (adapter, not copied) | ~30 | third-party MIT, invoked via CLI only | Zero-LLM script jobs: Hermes calls `dotz tick`. |
| Local due-time computation | SHEPHERD `core/scheduler.py` (`is_due`, persisted `next_check_at`) | EXTRACT_GENERIC (pattern) | ~40 | Apache-2.0, author-owned | `once` / `interval` / `cron` (cron via `croniter`, same lib HERMES uses). |
| Event envelope + idempotent ingest | SHEPHERD `storage/sqlite.py` (append-only events, `UNIQUE(source_event_id)`, immutability triggers) | EXTRACT_GENERIC | ~40 | Apache-2.0, author-owned | |
| Lifecycle state machine | SHEPHERD `core/state_machine.py` (explicit `TRANSITIONS` table) | EXTRACT_GENERIC (pattern) | ~20 | Apache-2.0 | Dot states: CREATED/IDLE/RUNNING/WAITING/BLOCKED/FAILED/STOPPED. |
| Task database | HERMES `kanban.db`; SHEPHERD actions table | NEW_CODE_REQUIRED (small) | ~60 | none | Kanban is Hermes-internal & multi-user-board shaped; coupling core Dotz to it would make Hermes mandatory. Simple SQLite table instead. |
| Persistent memory | HERMES memory plugins | NEW_CODE_REQUIRED (small) | ~30 | none | Notes are files in the Dot dir; history/receipts in SQLite. `MemoryStore` protocol allows later backends. |
| Model router (tier intent → provider) | NOVA `router/usage_aware.py` (`choose_worker`) | NEW interface + PRIVATE_DO_NOT_COPY | ~90 | NOVA routing is proprietary | Dotz ships a tier interface + OpenAI-compatible provider. NOVA (or any OpenAI-compatible router) plugs in by URL. |
| Model escalation | HARNESS frontier-plan / cheap-worker split | EXTRACT_GENERIC (policy) | ~15 | author-owned | Deterministic: escalate after repeated unparseable output or explicit `escalate`. |
| Hermes plugin | HARNESS `plugin/__init__.py` (`register(ctx)`, `ctx.register_tool`) | WRAP_EXISTING (pattern) | ~70 | HERMES API (MIT), no code copied | Tools: `dots_list/create/assign/status/result/steer/stop/history`. |
| Hermes Project identity | HARNESS `plugin/projects.py` (read-only `projects.db`) | EXTRACT_GENERIC | ~20 | author-owned | Optional; only when running inside Hermes. |
| Plugin discovery | HERMES plugin loader | REUSE_AS_IS | 0 | none | Dotz ships a standard Hermes plugin directory. |
| Structured tool schemas | HARNESS tool schema shape | EXTRACT_GENERIC | — | none | |
| MCP adapter | FIREWALL `mcp/server.py`, DREX-MCP | REUSE_AS_IS (documented) | 0 | none | Not duplicated in v0.1; firewall MCP already exists. |
| Observability | HARNESS `observability.py`, OBSERVATORY | not reused in v0.1 | 0 | — | CLI `history`/`inspect` over receipts is enough for v0.1. |
| Budget primitives | FIREWALL `shell.max_runtime_seconds` etc. (resource bounds only) | SMALL_EXTENSION | ~25 | none | Daily USD + per-task action caps computed from receipts. |
| Delegation | — | NEW_CODE_REQUIRED (small) | ~40 | none | `child_caps ⊆ parent.delegate`; enforced per task by the Drex gate. |
| CLI | FIREWALL/SHEPHERD argparse/click CLIs (style) | NEW_CODE_REQUIRED | ~200 | none | stdlib `argparse` (no new dep). |
| NOVA worker routing / prompts / policies | NOVA `router/`, `nova-harness` plugin | **PRIVATE_DO_NOT_COPY** | 0 | proprietary | Adapter only talks to an endpoint URL. |
| FETT, Money/Treasury, trading, wallet, lead engine, customer data | PRIVATE | **PRIVATE_DO_NOT_COPY** | 0 | proprietary | Not referenced. |

## Hard-rule answers ("does a working version already exist?")

- **Policy engine** — yes (FIREWALL). Reused; no second engine. The Dotz capability gate only decides *which guarded adapter a Dot may reach* (same role as HARNESS's fixed action list); all path/command/domain policy is FIREWALL's.
- **Scheduler** — yes (HERMES cron). Wrapped via `--no-agent --script`. A minimal `dotz tick` (persisted next-due, SHEPHERD pattern) exists so Dotz works without Hermes; it has no daemon of its own beyond an optional `while sleep` loop.
- **Model router** — yes, but NOVA's is private and machine-specific. Dotz defines only the tier interface.
- **Task DB / session registry / workspace manager** — HERMES has them but coupling would make Hermes mandatory; Dotz uses one small SQLite file and inherits Hermes Project identity when available.
- **Receipt system** — FIREWALL audit DB reused; Dotz adds a thin receipt row linking firewall `action_id` to dot/task/model/cost.
- **Shell wrapper** — FIREWALL ShellAdapter reused.
- **Plugin system** — HERMES plugin loader reused.
