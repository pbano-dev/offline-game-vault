# ADR 0006 — Generated files are a CI failure

- Status: **Accepted**
- Date: **2026-07-17**

## Context

An earlier source delivery caused Python-generated `egg-info` and
`__pycache__` paths to be committed. `.gitignore` prevents normal accidental
addition, but it does not remove already tracked files and can be bypassed.

Generated files also make preservation-oriented source archives depend on the
local interpreter, build path, and execution history.

## Decision

Generated and temporary paths are forbidden.

CI performs two checks:

```text
Git-tracked paths before dependency installation
complete work tree after validation and tests
```

The project is not installed in editable mode during CI. Tests use:

```text
PYTHONDONTWRITEBYTECODE=1
PYTHONPATH=src:tools
python -B
```

Repo-ready delivery archives must pass both a filesystem scan and a scan of a
freshly extracted ZIP before release.

## Consequences

- `__pycache__`, `.pyc`, `.egg-info`, tool caches, and temporary files cannot
  pass CI.
- CI also detects artifacts created by its own commands.
- The checked-out source tree remains clean after tests.
- A legitimate future need for one of the forbidden names requires an explicit
  ADR and checker change rather than a silent exception.
