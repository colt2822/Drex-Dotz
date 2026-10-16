# Drex Fleet v1

Runs beside the existing single Sales Dot sender and the discovery-v2 service. It never builds a Gmail client, never
calls the sender's tick, and never runs discovery jobs: the fleet observes those two processes and acts only through
existing adapters (`CrmStore.add_prospect` + `SalesWorker.draft_for`) when promoting.

* `python -m dotz.fleet run` — supervisor (`drex-fleet.service`): scheduler, one worker thread per operational dot, reaper. Single instance via flock.
* `python -m dotz.fleet dashboard --port 8795` — DREX FLEET COMMAND (`drex-fleet-command.service`), GET-only, 127.0.0.1 only, Host-header checked.
* `python -m dotz.fleet promote [--execute] [--limit N] [--domains a,b]` — enqueue a reverify (+ promote) task for the `sales` dot. Dry run by default.
* `python -m dotz.fleet status | enqueue <dot> <kind>`

State: `~/.local/state/dotz/fleet.db` (queue, workers, append-only attempts, effects ledger, snapshots) and
`~/.local/state/dotz/fleet-runtime/` (Drex per-Dot gates and append-only receipts). Neither is committed.

Queue semantics: UNIQUE idempotency key; claim = single `UPDATE … RETURNING`; lease + heartbeat extension (bounded by the
adapter timeout); fenced `complete`/`fail` on (owner, attempt); exponential-backoff retries; `DEAD` after `max_attempts`;
expired leases reaped back to `QUEUED`. Side effects are guarded by `fleet_effects` so a crash-retry cannot repeat them.

Completion: an adapter's `verify()` re-checks real state; only then is a task `SUCCEEDED`. `Runtime.execute_task` no
longer treats model text as completion (`UNVERIFIED` unless a verifier passes).

Dots without an adapter (director, research, growth, commerce, support) are registered `NOT_OPERATIONAL` and refuse tasks.
