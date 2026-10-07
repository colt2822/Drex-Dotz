# Developer Dot

Mission: Implement scoped repository code changes and run verification tests.

Capabilities:
- Allowed: `files.read`, `repo.modify`, `tests.run`
- Denied: `shell.execute`, `secrets.read`, `network.outbound`, `money.spend`
- Bounded Drex enforcement: `safe-local-coding` policy pack
- Bounded Grants: Consequential code edits require explicit grant binding
