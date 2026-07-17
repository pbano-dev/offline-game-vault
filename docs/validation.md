# Repository validation

Run locally from the repository root:

```bash
python -m pip install -r requirements-ci.txt
python tools/validate_repository.py
```

The validator checks:

- all JSON Schemas, including the vault inventory schema, are valid Draft 2020-12 schemas;
- each fixture `capsule.json` validates;
- host contracts, acceptance reports, and receipts validate;
- referenced documents and metadata files exist;
- profile dependencies reference existing object IDs;
- profile and object IDs are unique;
- host-contract platforms match their profiles;
- verified profiles have passing acceptance evidence;
- orphan contracts and acceptance reports are rejected;
- common absolute private-path patterns are rejected in fixture JSON.

This validation does not verify archived payload hashes because commercial
payloads are outside Git.
