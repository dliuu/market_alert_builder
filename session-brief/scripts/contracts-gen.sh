#!/usr/bin/env bash
# Regenerate both language bindings from the single BriefObject JSON Schema.
# CI runs this and then `git diff --exit-code`, so output must be deterministic
# (note --disable-timestamp below — without it every run dirties the file).
set -euo pipefail
cd "$(dirname "$0")/.."

SCHEMA="packages/contracts/brief-object.schema.json"

echo "→ TypeScript types  → apps/web/lib/contracts/brief.ts"
pnpm exec json2ts --input "$SCHEMA" --output apps/web/lib/contracts/brief.ts

echo "→ Pydantic models   → apps/worker/contracts/brief.py"
( cd apps/worker && uv run datamodel-codegen \
    --input "../../$SCHEMA" \
    --input-file-type jsonschema \
    --output contracts/brief.py \
    --output-model-type pydantic_v2.BaseModel \
    --use-standard-collections \
    --use-union-operator \
    --disable-timestamp )

echo "✓ contracts generated"
