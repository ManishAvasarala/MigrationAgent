# Canonical Spec → Workato Mapping

When `generate_workato.py` reads a migration spec, it translates each canonical
concept into a Workato recipe step. This table defines those mappings.

---

## Trigger Mapping

| Canonical `type` | Workato Trigger | provider | name | Notes |
|---|---|---|---|---|
| `http_listener` | Callable recipe HTTP trigger | `workato` | `callable_recipe` | Exposes recipe as HTTP endpoint; path set via `request_url_suffix` |
| `scheduler` | Scheduled trigger | `clock` | `scheduled_event` | Cron-based; default 1-hour |
| `event_trigger` | Event-driven (future) | TBD | TBD | Not yet implemented |
| `connector_trigger` | Manual review required | — | — | Flagged in output |

### Callable Recipe HTTP Endpoints
Workato callable recipes expose HTTP endpoints via the format:
```
https://www.workato.com/webhooks/rest/<user-id>/<recipe-endpoint-name>
```
The `request_url_suffix` in the trigger input sets the path portion.

---

## Step Mapping

| Canonical `type` | Workato Action | provider | name | Notes |
|---|---|---|---|---|
| `db_select` | Search rows | `postgresql` | `search_rows` | WHERE clause from SQL params |
| `db_insert` | Insert row | `postgresql` | `insert_row` | Fields from SQL params |
| `db_update` | Update rows | `postgresql` | `update_rows` | SET fields + WHERE from SQL |
| `db_delete` | Delete rows (future) | `postgresql` | `delete_rows` | Flagged for review currently |
| `set_payload` | Return HTTP response | `workato` | `callable_recipe_response` | Static JSON body |
| `transform` | Manual step (note) | — | — | Boomi Maps have no direct equivalent; use Workato formula pills |
| `custom_script` (Groovy) | Manual step (note) | — | — | Groovy logic preserved as note; implement via Workato Ruby or custom connector |
| `set_variable` | (skipped / inline) | — | — | In Workato, use trigger_input pills directly |
| `choice_router` | IF/ELSE condition | `workato` | `if_condition` / `else_condition` | Branching logic |
| `http_request` | HTTP action (future) | `http` | `request` | Not yet fully implemented |
| `logger` | (skipped) | — | — | Workato has built-in audit log |
| `subprocess_call` | Call recipe (future) | `workato` | `call_recipe` | Not yet implemented |

---

## PostgreSQL Action Input Format

### search_rows (SELECT)
```json
{
  "schema_table_name": "public.<table>",
  "where_clause": "column = \"{{input.value}}\"",
  "input": [{"name": "value", "type": "string", "value": "#{trigger_input['value']}"}]
}
```

### insert_row (INSERT)
```json
{
  "schema_table_name": "public.<table>",
  "input": [
    {"name": "first_name", "value": "#{trigger_input['firstName']}"},
    {"name": "last_name",  "value": "#{trigger_input['lastName']}"}
  ]
}
```

### update_rows (UPDATE)
```json
{
  "schema_table_name": "public.<table>",
  "input": [{"name": "status", "value": "#{trigger_input['status']}"}],
  "where_clause": "id = \"{{input.id}}\"",
  "where_input": [{"name": "id", "type": "string", "value": "#{trigger_input['id']}"}]
}
```

---

## Workato Formula Pill Syntax

Workato uses `#{expression}` for dynamic values inside strings:
| Source | Workato Pill |
|---|---|
| HTTP query param `?status=...` | `#{trigger_input['status']}` |
| HTTP path param `:id` | `#{trigger_input['id']}` |
| POST body field `firstName` | `#{trigger_input['firstName']}` |
| Previous step output | `#{step_alias['field_name']}` |
| DB search result rows | `#{search_table['rows']}` |
| First DB row | `#{search_table['rows'][0]}` |
| Current timestamp | `#{now}` |

---

## Recipe Code Structure (Workato API format)

The `code` field sent to `POST /api/recipes` is a JSON string with this shape:
```json
{
  "number": 0,
  "provider": "workato",
  "name": "callable_recipe",
  "as": "callable_recipe",
  "keyword": "trigger",
  "dynamicPickListSelection": {},
  "toggleCfg": {},
  "input": {
    "http_method": "get",
    "request_url_suffix": "/customers",
    "response_type": "dynamic"
  },
  "block": [
    {
      "number": 1,
      "provider": "postgresql",
      "name": "search_rows",
      "as": "search_rows",
      "keyword": "action",
      "dynamicPickListSelection": {"schema_table_name": "customers"},
      "toggleCfg": {},
      "input": {
        "schema_table_name": "public.customers",
        "where_clause": "status = \"{{input.status}}\""
      },
      "uuid": "<uuid>"
    },
    {
      "number": 2,
      "provider": "workato",
      "name": "callable_recipe_response",
      "as": "return_response",
      "keyword": "action",
      "dynamicPickListSelection": {},
      "toggleCfg": {},
      "input": {
        "response_body": "#{search_rows['rows'].to_json}",
        "response_status_code": "200",
        "reply_content_type": "application/json"
      },
      "uuid": "<uuid>"
    }
  ],
  "uuid": "<uuid>"
}
```

---

## What Requires Manual Review in Workato

| Item | Why | Resolution |
|---|---|---|
| Groovy scripts | No direct equivalent in Workato | Rewrite logic in Ruby (Workato custom scripts) or use formula pills |
| Boomi Map components | Workato has no standalone mapping step | Use formula pills on individual fields in insert/update actions |
| Branching logic | IF/ELSE structure must be rebuilt | Use Workato IF/ELSE condition blocks |
| Set Properties steps | DDP/DPP concept doesn't exist in Workato | Use formula pills referencing trigger_input directly |
| Complex SQL with joins | Workato's search_rows may not support complex joins | Use raw SQL query action or split into multiple steps |

---

## Workato API Reference

| Operation | Endpoint |
|---|---|
| List folders | `GET /api/folders` |
| Create folder | `POST /api/folders` |
| List connections | `GET /api/connections` |
| Create connection | `POST /api/connections` |
| List recipes | `GET /api/recipes` |
| Create recipe | `POST /api/recipes` |
| Get recipe | `GET /api/recipes/{id}` |
| Start recipe | `PUT /api/recipes/{id}/start` |

Authentication: `Authorization: Bearer <api_token>` header.
Rate limits: 60 req/min for reads, 1 req/sec for connection creation.
