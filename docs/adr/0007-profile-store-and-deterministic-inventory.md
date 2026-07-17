# ADR 0007 — Profile-level store operations and deterministic inventory

- Status: **Accepted for implementation v0**
- Date: **2026-07-17**

## Context

Single-object ingestion does not prove that an execution profile has all of
its dependencies. The archive also needs an inventory that can be regenerated
without false changes from timestamps or host paths.

## Decision

Add:

```text
ogv ingest-profile
ogv verify-profile
ogv inventory
```

`ingest-profile` accepts explicit `OBJECT_ID=PATH` assignments only for
dependencies of the selected profile. Its report omits host paths.

`verify-profile` verifies every dependency without modifying the vault.

`inventory` hashes every canonical object and emits deterministic JSON with:

```text
digest
relative content-addressed path
exact byte count
```

It contains no timestamp, username, hostname, vault path, or source path.

## Consequences

- One operation establishes the complete immutable set for a profile.
- Repeated ingestion is idempotent.
- An unchanged vault produces byte-identical inventory JSON.
- Inventory generation is intentionally expensive because it verifies content.
- Materialization can require successful profile verification first.
