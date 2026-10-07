# Drex Dotz

> **Persistent autonomous AI workers with bounded Drex capabilities.**  
> *Give agents jobs, not root access.*

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Drex Dotz provides persistent, autonomous workers ("Dotz") that operate under a strict **capability firewall**. Instead of receiving unconstrained shell, filesystem, or network access, every consequential action flows through Drex:

```text
Dot
→ capability request
→ Drex
→ ALLOW / BLOCK / bounded grant
→ execution
→ receipt
```

---

## Architecture

```text
                    USER / HERMES / API
                           │
                           ▼
                       DOT RUNTIME
                           │
              identity / lifecycle / events
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
      Research Dot      Work Dot       Operator Dot
          │                │                │
          └────────────────┼────────────────┘
                           ▼
                         DREX
                    capability firewall
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
           files         shell          web/API
        workspace       commands       connectors
                           │
                           ▼
                        RECEIPTS
```

---

## 5-Minute Quickstart

### 1. Installation

```bash
# Install with drex-agent-firewall
pip install drex-dotz
```

### 2. Initialize and Seed Example Workers

```bash
dotz init
dotz list
```

Output:
```text
DOT                  STATE        UPDATED
-------------------------------------------------------
developer            IDLE         1760000000.0
operator             IDLE         1760000000.0
researcher           IDLE         1760000000.0
```

### 3. Inspect Researcher Dot

```bash
dotz show researcher
```

Output:
```text
Researcher status: IDLE

Workspace:
  ./dots/researcher/workspace

Model policy:
  default: cheap
  escalate: frontier

Capabilities:
  ✓ web.read
  ✓ files.read
  ✓ notes.write
  ✗ shell.execute
  ✗ secrets.read
  ✗ filesystem.modify
  ✗ money.spend
  ✗ filesystem outside workspace

Drex:
  enforcement active (pack: read-only-research)
```

### 4. Assign and Run a Task

```bash
dotz assign researcher "Investigate security logs and summarize notes" --exec
```

### 5. Inspect Receipts

Every consequential action generates an audit receipt:

```bash
dotz history researcher
```

---

## The Core Differentiator: Bounded Drex Grants

A Dot never gets arbitrary host tools. When a developer Dot needs to modify files:
1. It requests an operation under an allowed capability.
2. For consequential code mutation, a task-scoped, time-limited (`TTL`), and resource-bound **Grant** is issued.
3. If an action attempts to step outside the workspace, read credentials like `~/.ssh`, or exceed delegated authority, Drex **BLOCKS** it before any execution happens.
4. An immutable audit receipt is persisted with secret redaction.

---

## Included Example Dotz

- **Researcher**: Web research, file reading, notes creation. No shell or arbitrary filesystem mutation.
- **Developer**: Workspace code editing and tests execution bounded by explicit mutation grants.
- **Operator**: Bounded service inspection and log analysis without generic root access.

---

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
