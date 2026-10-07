# Drex Dotz v0.1.0

Initial public open-source release of **Drex Dotz**: persistent autonomous AI workers with bounded capabilities.

Give agents jobs, not root access.

## Features

- **Persistent Autonomous Dot Workers**: Workers maintain lifecycle states (`IDLE`, `RUNNING`, `STOPPED`) and persistent state across restarts in SQLite.
- **Drex-Bounded Capabilities**: Every consequential action flows through the **Drex Agent Firewall** (`drex-agent-firewall v0.1.3`). Hard invariants block workspace escapes, credential reading, and arbitrary shell execution before execution can occur.
- **Bounded Grants**: Tasks needing consequential mutations require task-scoped, resource-bounded, and time-limited (`TTL`) capability grants.
- **Schedules & Event Wakeups**: Integrated interval and cron scheduling triggers, plus append-only idempotent event queues.
- **Delegation**: Strict hierarchy where parent Dots can delegate subsets of their own capabilities to child Dots with enforced ceiling validation.
- **Immutable Receipts**: All evaluated actions emit tamper-evident audit receipts with automated secret redaction.
- **Reference Examples**:
  - `researcher`: Safe documentation and research agent with read-only access.
  - `developer`: Code editing bounded by explicit mutation grants.
  - `operator`: Bounded status inspection and diagnostic tasks.
- **Optional Integrations**: Adapters for NousResearch Hermes Agent plugins and NOVA workflows.

## Installation

Install directly from GitHub via pip (no PyPI required):

```bash
pip install "git+https://github.com/colt2822/Drex-Dotz.git@v0.1.0"
```

Or with explicit dependency ordering:

```bash
pip install "git+https://github.com/colt2822/Drex-Agent-Firewall.git@v0.1.3"
pip install "git+https://github.com/colt2822/Drex-Dotz.git@v0.1.0"
```

## Known Limitations

- Enforcement operates at the Python runtime, capability gate, and guarded adapter layers; it does not replace kernel cgroups or virtual machine sandboxing.
- State persistence is local to the machine's SQLite database; distributed multi-machine synchronization is deferred to future releases.
