# ADR 0016 — State-free component composition and archived runtime state

Status: **accepted and implemented**

Date: 2026-08-03

## Context

Offline Game Vault preserves legally acquired Windows games as immutable,
content-addressed objects and materializes writable derivatives for execution.
During the first implementation of user-requested compositions, several
different concerns were represented as if they were one decision:

- a profile recipe;
- the historical result of a test;
- permission to assemble a combination;
- ownership of a reusable runner or runtime;
- the operational state created after materialization.

That coupling made the model harder to extend and risked treating incomplete
historical evidence as either an authorization token or a prohibition. It also
made reusable UMU pieces appear to belong to a game profile even though the
UMU/Python backend, Proton runner, and Steam Linux Runtime are independent
preserved components.

A second problem appeared in generated UMU derivatives. The archived Steam
Runtime `var` subtree was required to become empty. The generated sanitizer
therefore removed every first-level entry, including data whose
regenerability had not been established. The immutable CAS object remained
safe, but the derived tree no longer reproduced all archived inputs.

Finally, the branch changelog advanced to 0.11.4 while the Python package and
project metadata still reported 0.11.3. Receipts generated from that code
would therefore identify the wrong orchestrator version.

## Decision

### Profiles are recipes, not permission records

Profiles describe how preserved pieces can be assembled and launched. They do
not carry maturity, recommendation, acceptance, or authorization state.

The absence of a previously tested exact combination does not prohibit a user
from requesting it. The core may block only for technical facts such as a
missing object, digest mismatch, unsafe archive, incompatible runner, absent
runtime family, ambiguous source recipe, or unsafe destination.

### Acceptance evidence is independent

Acceptance reports remain historical evidence about an exact game, component
set, host contract, and test date. They are validated independently, but they
are not copied into generated profiles and do not permit or forbid
materialization.

Materialization success proves structural assembly and verification. It does
not prove gameplay, save loading, DLC availability, controller behavior,
network containment, normal shutdown, or restoration on another host.

### Reusable execution pieces are global components

The UMU/Python backend, Proton runner, and Steam Linux Runtime are registered
and verified independently against the global index, immutable inventory, CAS
path, recorded size, and SHA-256.

A resolved UMU composition records:

- `component_set_id`;
- backend component identity;
- Proton runner identity;
- Steam Linux Runtime component identity and family;
- one deterministic preserved UMU entrypoint.

The launcher executes the entrypoint selected during archive inspection. It
does not rediscover a different executable after materialization.

### Original objects and writable derivatives remain distinct

The CAS object is the preserved original. A materialization is a writable
derivative assembled from verified objects. Changes made during execution do
not alter the original object, but the initial derivative must still reproduce
the archived inputs unless a transformation is explicitly justified.

### Archived runtime state is preserved by default

The Steam Runtime `var` subtree may contain runtime data, references, or state
whose purpose cannot be inferred safely from its name. Therefore:

- a non-empty archived `var` subtree is valid;
- materialization and verification require the subtree to exist as a regular
  directory, not to be empty;
- generated sanitizers remove only paths whose regenerability is known and
  documented;
- unknown archived entries survive materialization, sanitization, and
  verification.

At present, the generated composition sanitizer removes only Proton's known
`files/steampipe_fixups_mtime` marker. Any future normalization requires a
named path, a documented reason, and a regression test proving that unrelated
archived data survives.

### Pressure-vessel temporary roots use container link semantics

Immediate `runtime_var/tmp-*` directories may contain symlinks whose targets
are meaningful only inside pressure-vessel, including `/run/host` and
container-absolute paths. The host-side broken-link check therefore excludes
only those concrete temporary roots. Archive path safety remains mandatory,
and broken symlinks elsewhere remain blocking.

### Published version identifiers must agree

`pyproject.toml`, `offline_game_vault.__version__`, the leading changelog
release, CLI version output, and generated receipt version must identify the
same release. A regression test enforces this contract.

## Consequences

- The Vault stores pieces and facts, not opinions about maturity.
- Users may request new technically compatible compositions without rewriting
  source capsules.
- Acceptance remains useful evidence without becoming a gate.
- UMU dependencies remain globally reusable and independently verifiable.
- Generated launchers bind to inspected preserved entrypoints.
- Sanitization is an explicit, narrow transformation rather than a blanket
  deletion policy.
- The original CAS object and the writable derivative remain auditable as
  different objects.
- Versioned receipts can be traced back to the exact orchestrator release.

## Rejected alternatives

### Keep profile maturity as an authorization gate

Rejected because historical testing and technical compatibility answer
different questions. It would also prevent new combinations from being tested.

### Store reusable UMU pieces inside game profiles

Rejected because backend, runner, and Steam Runtime have independent identity,
integrity, and reuse boundaries.

### Search for an UMU executable at launch time

Rejected because the materialized tree could contain several candidates or a
different candidate from the one inspected. Binding the verified path is more
reproducible.

### Empty the complete runtime `var` subtree

Rejected because directory names are not evidence of regenerability and the
policy can remove archived data unrelated to the known mutable marker.

## Verification

The implementation is covered by tests that:

- resolve global UMU components and a deterministic entrypoint;
- execute the generated UMU launcher;
- retain synthetic archived data below `steamrt4/var`;
- remove the known Proton-generated marker without deleting unrelated data;
- verify the derivative after sanitization;
- validate independent acceptance reports;
- require project, package, and changelog versions to match.

Real-Vault validation additionally resolves the preserved UMU backend and
Steam Runtime from their physical immutable objects. Functional game
acceptance remains a separate per-game test.
