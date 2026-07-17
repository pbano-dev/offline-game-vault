# Generated-file hygiene

The repository must not track files produced by local Python execution or an
editable installation.

Remove these paths if they are already tracked:

```text
src/offline_game_vault.egg-info/
src/offline_game_vault/__pycache__/
tests/__pycache__/
```

The root `.gitignore` prevents their reintroduction.

These files are not source, evidence, fixtures, preserved payloads, or
reproducible build inputs.
