# ADR 0002 — Composite legacy bottle archives

- Status: **Accepted for schema generation 0**
- Date: **2026-07-17**

## Context

The first real fixture is a Bottles Full Archive that already contains:

- the Windows game payload;
- the Wine prefix;
- installed Windows prerequisites;
- configuration;
- saves;
- the active derived Steamworks replacement.

The initial schema assigned one role to each object. That model cannot describe a
legacy monolithic archive honestly without pretending that it contains only a game
or only a prefix.

## Decision

An archived object may declare multiple semantic `roles`.

Example:

```json
{
  "roles": [
    "game_payload",
    "prefix_baseline",
    "save",
    "configuration",
    "derived"
  ]
}
```

This does not prevent future normalization into smaller independent objects.

The object digest always identifies the whole archived byte sequence. Roles are
descriptive metadata and do not imply separate hashes for embedded content.

## Consequences

- Existing monolithic packages can be represented without immediate repacking.
- The materializer may initially copy or extract one composite object.
- Future migrations can split it into game, prefix, saves, and configuration objects.
- A composite object increases the failure domain and limits deduplication.
- Embedded persistent state must still be declared separately at the logical level.
