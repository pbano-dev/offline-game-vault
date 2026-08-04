# ADR 0015 — User-requested component compositions

Status: **accepted and implemented**

Date: 2026-08-01

## Context

Capsule profiles are recipes and provenance records. Acceptance reports
describe historical tests of exact combinations. Treating either as a
permission system would prevent users from assembling a technically valid
combination that still needs functional testing.

The Vault must remain offline and reproducible. Composition freedom therefore
cannot mean downloading a runner, using an unrecorded system Wine, or ignoring
object integrity.

## Decision

A user may request a Linux composition for Bottles, Direct-Wine, or UMU/Proton
whenever the required preserved objects are technically available.

Profiles do not contain maturity or authorization fields. Acceptance reports
remain independent evidence and do not authorize or prohibit a composition.

The core synthesizes a private operational profile without modifying the
published capsule. It retains the selected source recipe and component
provenance. For UMU, the generated host contract records the global UMU/Python
backend component, Proton runner, Steam Linux Runtime component, and resolved
component-set identifier. No acceptance result is copied into the generated
profile.

Only runners indexed as `shared-runner`, present in the immutable store, and
verified by size and SHA-256 are selectable. No runner is downloaded and no
system runner is used as a fallback.

Bottles receives a runner extracted from the preserved Vault object. An
existing Bottles runner directory is reusable only when it carries a matching
OGV installation marker and its complete tree still matches that marker.

UMU composition requires three independently preserved pieces:

1. a global UMU/Python backend component;
2. a Proton runner;
3. the exact Steam Linux Runtime family required by that runner.

Windows-native composition is outside this decision.

## Permitted blocking conditions

The core may reject an operation for material reasons, including:

- absent or hash-mismatched objects;
- unsafe archives, paths, links, or destination topology;
- a runner without the executables required by the selected backend;
- a missing preserved UMU/Python backend;
- a missing or incompatible Steam Linux Runtime family;
- an ambiguous source layout;
- an occupied or unsafe destination;
- a failed backend verification.

Missing acceptance evidence or the absence of an already declared exact
backend/runner recipe are not blocking conditions.

## Consequences

- A successful materialization proves structural assembly, not gameplay.
- A successful launch does not transfer evidence to another component set.
- Recommendations may remain useful defaults but are not permission tokens.
- Receipts record what was attempted and what completed.
- Integrity and offline self-containment remain mandatory.
