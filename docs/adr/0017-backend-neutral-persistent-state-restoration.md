# ADR 0017: Backend-neutral persistent-state restoration

- Status: Accepted
- Date: 2026-08-04
- Version: 0.12.0

## Context

Component composition already supported Bottles, Direct-Wine, and UMU, but the
public `--state-backup` path was implemented only for Direct-Wine. UMU also had
a separate low-level `state_archives` mechanism. That split forced callers to
know backend-specific state behavior and prevented the GUI from offering one
consistent restoration operation.

Persistent state is independent from profile maturity, functional acceptance,
network containment, and immutable game objects. The source capsule remains
authoritative for logical state declarations; adapters are authoritative for
the physical state root inside a derivative.

## Decision

`compose --state-backup <BACKUP>` is the public state-restoration contract for
Bottles, Direct-Wine, and UMU.

When the operational capsule declares at least one preservable state item:

1. composition requires a backup;
2. the backup is verified against that capsule;
3. the adapter resolves the effective state root;
4. restoration runs in staging with a mandatory pre-restore snapshot;
5. restoration evidence is written into the derivative;
6. verification checks that evidence;
7. publication occurs only after successful restoration and verification.

The effective roots are derived from contracts, not game identifiers:

```text
Direct-Wine: playable prefix
Bottles: staged bottle root
UMU: umu.paths.prefix propagated from playable.paths.prefix
```

An existing UMU derivative may be reused only when its recorded generic
baseline matches the selected backup. A different baseline requires a new
derivative or explicit removal.

The low-level `umu.state_archives` API remains available for compatibility but
cannot be combined with generic `--state-backup`.

Low-level adapter functions may still create a clean derivative without state
when explicitly invoked outside the composition boundary. This preserves their
existing role in diagnostics and tests; the public composition boundary remains
strict.

## Consequences

- The GUI can expose one save-selection flow for all three backends.
- The GUI does not resolve prefix or save paths.
- Capsules that declare preservable state cannot be composed without an
  explicitly selected verified backup.
- Backend receipts gain restoration provenance and removal guards.
- The source capsule and immutable Vault objects remain unchanged.
- Restoration success is not functional acceptance: loading the save in the
  game remains a separate per-title test.
- Network isolation remains independent from restoration and reproducibility.
