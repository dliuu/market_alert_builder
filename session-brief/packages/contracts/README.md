# @session-brief/contracts

The BriefObject is defined **once** here, as JSON Schema. Both languages are
generated from it — never hand-edit the generated files.

```
brief-object.schema.json
  ├── json-schema-to-typescript → apps/web/lib/contracts/brief.ts   (TS types)
  └── datamodel-code-generator   → apps/worker/contracts/brief.py    (Pydantic v2)
```

Regenerate both with `pnpm contracts:gen` from the repo root. CI runs the same
command and fails if the checked-in output is stale, so run it after any schema
change and commit the result. Bump `schema_version` on any shape change.
