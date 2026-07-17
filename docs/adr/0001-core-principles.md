# ADR 0001 — Core preservation and materialization principles

- Status: **Accepted for specification v0**
- Date: **2026-07-17**

## Context

The project must preserve games for long-term offline use while avoiding two failures:

1. duplicating every shared runtime inside every game;
2. creating fragile launch scripts tied to one host, username, path, or installed tool.

Existing preserved games already use different execution stacks, including Bottles and UMU. A future user should normally select a game and request “materialize and play”, without manually reconstructing the stack from a README.

## Decision

### A. The vault is immutable

The archived vault is never the working directory. Materialization happens on host-local storage or in a prepared portable export.

### B. Games are declarative capsules

Each game declares exact objects, profiles, requirements, persistent state, and acceptance status.

### C. Shared objects are content-addressed

Common runners, runtimes, tools, and sources may be stored once and referenced by SHA-256.

Logical self-containment does not require physical duplication inside every game directory.

### D. Host behavior is implemented by adapters

Bottles, Wine, UMU, and Windows details are isolated behind adapters.

Game manifests do not hardcode private absolute paths or assume a tool is already installed.

### E. Binary-first recovery

The normal path uses the exact archived and verified binary.

Recompilation is a recovery path, not the default installation path.

### F. Golden state is not daily state

A baseline bottle or prefix is copied or snapshotted before use. Saves and explicitly persistent state survive rematerialization.

### G. No silent substitution

Missing or incompatible dependencies produce an explicit failure or an explicitly selected alternative profile.

### H. Verification never regenerates

Verification compares against the sealed baseline. Regeneration creates a new baseline and requires a deliberate operation.

### I. Portable export is first-class

The system can export a selected game plus all required dependencies to removable media. The portable export remains immutable and normally materializes to the destination host before execution.

### J. Human documentation remains mandatory

Automation does not replace the per-game README, game sheet, credits, preservation authorship, evidence, or limitations.

## Consequences

### Positive

- shared runtimes are not duplicated unnecessarily;
- a game can support several independent recovery profiles;
- the vault remains safe from normal execution;
- host-specific paths and tools can evolve without rewriting game metadata;
- USB and optical exports can be generated from the same dependency graph.

### Negative

- the project must maintain schemas and adapters;
- a portable export may be larger than the compact collection representation;
- compatibility checks cannot guarantee future execution;
- strict resolution may fail where an opportunistic launcher might appear to work.

## Rejected alternatives

### One giant autonomous package per game

Rejected as the default because it duplicates engines and runtimes, complicates M-Disc use, and wastes storage.

### One shared mutable installation

Rejected because updates or corruption could affect several games and destroy reproducibility.

### README-driven restoration

Rejected as the normal interface because it depends on future manual reconstruction. Retained only as recovery documentation.

### Compile everything during materialization

Rejected because future toolchains and build systems are less predictable than archived verified binaries.
