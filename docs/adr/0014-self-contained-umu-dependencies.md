# ADR 0014: self-contained UMU dependencies

- Status: accepted
- Date: 2026-07-30
- Schema generation: 0

## Context

A restored UMU runtime can preserve its own archive while still containing
absolute symlinks to Vulkan layers or other components installed on the host.
Such a materialization is reproducible only on a machine with the same external
files and can disclose private host paths.

## Decision

Treat a required external regular file as a profile dependency only after it is
explicitly classified and authorized.

The workflow is:

```text
detect external target
→ capture authorized files into a separate immutable object
→ verify digest, size, architecture, and archive round trip
→ add the object to the profile
→ rewrite targets as relative internal symlinks
→ verify with original host paths hidden
```

Absolute external targets are not accepted merely because the game launches.

Update suppression and network isolation remain separate controls.

## Consequences

- Self-containment is demonstrated rather than assumed.
- Host-path privacy improves.
- Shared dependencies can be deduplicated by digest.
- Redistribution rights must be evaluated independently.
- Different games may reveal different host dependencies and still require
  title-specific functional acceptance.
