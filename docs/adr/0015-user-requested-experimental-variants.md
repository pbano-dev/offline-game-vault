# ADR 0015 — User-requested experimental variants

Status: **accepted and implemented**

Date: 2026-08-01

## Context

Capsule profiles and acceptance receipts describe combinations that have been
tested. Treating `candidate`, `not_tested`, `unavailable`, or the absence of an
exact profile as authorization decisions prevents the user from materializing
the very combination that needs to be tested.

The Vault must remain offline and reproducible. Experimental freedom therefore
cannot mean downloading a runner, using an unrecorded system Wine, or ignoring
object integrity.

## Decision

A user may request an experimental Linux variant for Bottles, Direct-Wine, or
UMU/Proton whenever the required preserved objects are technically available.

Profile status and acceptance are descriptive:

- they select recommendations and report evidence;
- they never authorize or prohibit an experimental materialization;
- no acceptance is inherited when backend, runner, runtime, or source profile
  changes.

The core synthesizes a private operational profile without modifying the
published capsule. The resulting backend receipt records:

- the source profile;
- the selected backend;
- the selected preserved runner;
- an automatically resolved shared UMU runtime when applicable;
- `kind: experimental`;
- `acceptance_inherited: false`.

Only runners indexed as `shared-runner`, present in the immutable store, and
verified by size and SHA-256 are selectable. No runner is downloaded and no
system runner is used as a fallback.

Bottles receives a runner extracted from the preserved Vault object. An
existing Bottles runner directory is reusable only when it carries a matching
OGV installation marker and its complete tree still matches that marker.

UMU synthesis requires two preserved pieces:

1. a Proton runner;
2. a reusable, content-addressed UMU/Python/Steam Linux Runtime object.

Windows-native synthesis is outside this decision.

## Permitted blocking conditions

The core may reject an operation for material reasons, including:

- absent or hash-mismatched objects;
- unsafe archives, paths, links, or destination topology;
- a runner without the executables required by the selected backend;
- a missing preserved UMU backend;
- an ambiguous source layout or missing shared runtime;
- an occupied or unsafe destination;
- a failed backend verification.

Lack of acceptance, `candidate`, `not_tested`, `unavailable`, or absence of an
already declared exact backend/runner pair are not blocking conditions.

## Consequences

- A successful materialization proves structural assembly, not gameplay.
- A successful launch still does not transfer acceptance to another variant.
- Recommendations remain useful defaults but are always overridable.
- Receipts become evidence of what was attempted rather than permission tokens.
- Integrity and offline self-containment remain mandatory.
