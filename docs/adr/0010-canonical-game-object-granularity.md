# ADR 0010: One canonical game object per title

- Status: accepted
- Date: 2026-07-21
- Schema generation: 0
- Public baseline reviewed: `5ee2bd945286e48f93cb4ec502f94051785b8863`

## Context

Early generation-0 fixtures declared original executables, prepared
executables, Steamworks DLLs, and local Steamworks reimplementations as
independent vault objects even when those exact bytes were already preserved
inside a verified game archive.

That representation duplicated data and confused two identities:

1. the first-class archival object resolved and materialized by the
   orchestrator;
2. a critical file embedded inside that object whose digest must still be
   retained and verified.

The collection model uses one self-contained object per game and one object per
shared runner or runtime package.

## Decision

Each capsule has exactly one non-shared first-class object carrying the
`game_payload` role.

Additional first-class objects are allowed only as shared runner or runtime
archives. Critical files contained by the game object are represented by
`embedded_artifacts` and, for playable profiles, by `protected_files`.
Embedded artifacts retain IDs, digests, sizes, roles, descriptions, and the ID
of the game object that contains them. They are not dependencies and are not
materialized independently.

The semantic repository validator enforces this rule. JSON Schema describes
the `embedded_artifacts` structure; cross-field cardinality and dependency
rules remain validator responsibilities.

## Migration

The public DSR and Sekiro fixtures retain only:

```text
canonical game object
shared GE-Proton runner object
```

Their former executable and DLL object records move to
`embedded_artifacts` without changing any digest.

The migration does not alter payloads, runners, acceptance evidence, profile
status, launch metadata, or protected-file identities.

## Consequences

- adding a game normally adds one game object;
- an already archived runner is shared rather than duplicated;
- portable export still includes the game object and every shared execution
  dependency referenced by the selected profile;
- original and derived identities remain auditable without inflating the
  object store;
- fixtures that reintroduce embedded files as first-class objects fail
  repository validation and unit tests.
