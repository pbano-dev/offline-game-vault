# ADR 0003 — Python stdlib-first orchestrator

- Status: **Accepted for implementation v0**
- Date: **2026-07-17**

## Context

The project needs one orchestrator that can inspect capsules, probe hosts,
materialize objects, launch profiles, and write receipts on Linux and Windows.

The archive must remain understandable and recoverable in the future. Core
operations should not depend on a large framework or an online package service.

## Decision

The first orchestrator is a Python package with the command name `ogv`.

Rules:

1. Python `3.11+`.
2. Standard library for core planning and materialization.
3. Optional dependencies are limited to development, validation, or adapters
   that demonstrably need them.
4. The CLI returns non-zero on uncertainty or missing required objects.
5. `ogv plan` is read-only.
6. A destination inside the immutable vault is rejected.
7. Missing objects are fatal unless the user explicitly selects
   `--allow-missing`.
8. Paths are discovered or provided at runtime; private host paths are never
   encoded in capsules.
9. Adapter execution will be added only after planning and receipts are stable.

## Consequences

- The source remains small and auditable.
- The command can run from an archived virtual environment or bundled Python.
- Planning can be tested before implementing destructive operations.
- A future standalone executable remains possible.
- Python itself must eventually be included in portable exports where the host
  contract cannot assume it.
