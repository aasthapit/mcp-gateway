# schemas

JSON Schema (draft-04 subset) for the policy collections in MongoDB database `mcp_gateway`.
These files are the **single source of truth**:

| File                    | Collection  | Consumers                                                |
| ----------------------- | ----------- | -------------------------------------------------------- |
| `upstreams.schema.json` | `upstreams` | gateway (pydantic), admin-api (ajv), Mongo `$jsonSchema` |
| `toolsets.schema.json`  | `toolsets`  | same                                                     |
| `grants.schema.json`    | `grants`    | same                                                     |
| `meta.schema.json`      | `meta`      | same                                                     |

`build-mongo-validators.py` translates them into `local/mongo-init/validators.js` (see the
docstring for the draft-04 → `$jsonSchema` rules: `integer` → `int|long`, `date-time` → BSON
`date`, no `$ref`). Regenerate with `make validators`; CI fails if the generated file is stale.

Keep the files draft-04 compatible: no `$ref`, `const`, `if/then`, `dependentRequired`, etc.
