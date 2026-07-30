# DSR UMU self-contained reference

## Purpose

*DARK SOULS REMASTERED* was used as a private, legally owned reference case to
test a reproducible UMU/Proton package. The public repository retains only this
sanitized technical account. It contains no game payload, save, private
identity, runner, runtime, proprietary binary, or private path.

## Problems revealed

The reference case exposed four generic issues:

1. Steam Linux Runtime regenerated a volatile `var/tmp-*` token after launch.
2. Exact hardlink topology needed direct manifest verification.
3. Four Vulkan-layer symlinks resolved to files from the host Steam
   installation.
4. Those absolute targets also leaked the private host layout into evidence.

## Generic solution

The runtime sanitizer normalizes only the declared volatile token and then
rechecks the archived topology.

The profile declares exact symlink and hardlink manifests. The reference
runtime retained 1,295 symlinks and 6,037 hardlink groups after execution.

The four required Vulkan layer files were captured as a separate immutable
dependency, verified by digest and archive round trip, and referenced through
relative symlinks inside the materialization.

## Acceptance boundary

A clean restoration was executed with:

- external network access unavailable;
- original host Vulkan-layer directories hidden;
- preserved state loaded;
- gameplay reached;
- normal close;
- post-run protected-file and topology verification.

This validates the exact private DSR profile used for the test. It does not
validate a different game, runner, runtime, object set, or platform.

## Reusable conclusions

Reusable mechanics:

- content-addressed restoration;
- offline runtime and cache deployment;
- symlink and hardlink preservation;
- volatile-runtime normalization;
- external-target detection;
- authorized dependency internalization;
- privacy auditing;
- clean restoration and post-run verification.

Game-specific evidence remains required for executables, protection systems,
DLC, saves, input behavior, gameplay, and online requirements.
