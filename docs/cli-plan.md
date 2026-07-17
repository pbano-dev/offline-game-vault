# `ogv plan`

`ogv plan` creates a read-only materialization plan.

Example:

```bash
ogv plan   --capsule fixtures/dark-souls-remastered/capsule.json   --profile linux-bottles-flatpak   --vault-root /path/to/immutable-vault   --destination /path/to/working/dsr   --json
```

The command:

- selects exactly one execution profile;
- resolves each dependency by object ID;
- maps content-addressed archive paths under the vault root;
- records whether each required object is present;
- chooses a preliminary operation strategy;
- loads the referenced host-contract path;
- rejects a destination inside the vault;
- performs no copy, extraction, installation, or launch.

By default, a missing required object is an error. For metadata-only inspection:

```bash
ogv plan ... --allow-missing
```

This option does not make a capsule runnable. It only keeps missing objects
explicit in the generated plan.
