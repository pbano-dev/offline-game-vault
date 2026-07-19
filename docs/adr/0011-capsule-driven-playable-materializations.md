# ADR 0011: capsule-driven playable materializations

Status: Accepted
Date: 2026-07-19

## Context

The Sekiro proof of concept demonstrated a complete direct-Wine cycle:

```text
verify objects
→ materialize
→ restore accepted state
→ launch twice
→ record normal exit
→ detect changed save
→ export state
→ remove safely
```

Copying that title-specific script into the repository would duplicate the
existing verified materializer, safe tar extractor, receipt logic, and state
manager.

## Decision

Add a declarative `playable` contract to execution profiles and a direct-Wine
adapter that composes the existing engines.

The contract contains archive-root mappings, final paths, explicit prefix
operations, and protected-file identities. The shared engine contains no
Sekiro or DSR constants.

Each materialization receives a standard-library-only portable runtime and
generated launchers. Removal is state-aware and aborts on changed state unless
it is exported or explicitly discarded.

## Consequences

Positive:

- title-specific paths become capsule data;
- existing safety and transaction code is reused;
- materializations remain usable without Bottles;
- launchers are suitable for desktop shortcuts;
- CLI operations can later be exposed as GUI jobs.

Limitations:

- Python 3 is a host prerequisite;
- generation `0` supports direct Wine on Linux only;
- network isolation is rejected rather than simulated;
- portable runtime receipts are integrity records, not signatures;
- cross-host and long-term compatibility require separate acceptance.
