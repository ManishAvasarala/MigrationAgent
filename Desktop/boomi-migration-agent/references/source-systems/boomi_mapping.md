# Boomi → Canonical Spec Mapping

When `analyze_boomi.py` reads Boomi process XML files, it translates each shape into
the platform-agnostic migration spec format. This table defines what each Boomi construct
maps to in the canonical spec.

---

## Trigger Shapes

| Boomi Shape | Boomi Connector Type | Canonical `type` | Notes |
|---|---|---|---|
| Start (WSS Listen) | `wss` | `http_listener` | operationType maps to HTTP method (see below) |
| Start (Scheduler) | `schedule` | `scheduler` | Trigger frequency extracted from schedule component |
| Start (Event Streams) | `officialboomi-X3979C-events-prod` | `event_trigger` | Topic and group captured |
| Start (Other) | any | `connector_trigger` | Flagged `requires_review: true` |

### WSS operationType → HTTP Method
| Boomi operationType | HTTP Method |
|---|---|
| QUERY | GET |
| CREATE | POST |
| UPDATE | PUT |
| DELETE | DELETE |

### WSS Path Inference
Boomi WSS URL: `/ws/simple/{operationType.lower()}{ObjectName}` (e.g., `/ws/simple/queryCustomers`)
Canonical `path`: converted to clean REST path by the analyzer (e.g., `/customers`)
- Object names ending with `ById` → `/resource/{id}` (singular resource with ID path param)
- Plural object names → `/resources` (collection)
- Singular → pluralized to `/resources`

---

## Step Shapes

| Boomi Shape Type | Shapetype | Canonical `type` | Key Fields Extracted |
|---|---|---|---|
| Connector Action (DB GET) | `connectoraction` + dbv2 GET | `db_select` | sql, table, params, connection_id |
| Connector Action (DB INSERT) | `connectoraction` + dbv2 CREATE | `db_insert` | sql, table, params, connection_id |
| Connector Action (DB UPDATE) | `connectoraction` + dbv2 UPDATE | `db_update` | sql, table, params, connection_id |
| Connector Action (DB DELETE) | `connectoraction` + dbv2 DELETE | `db_delete` | sql, table, params, connection_id |
| Connector Action (REST) | `connectoraction` + rest/http | `http_request` | operation_id, connection_id |
| Map | `map` | `transform` | map_id (references map component) |
| Data Process (Groovy) | `dataprocess` | `custom_script` | language=groovy, script, purpose |
| Message | `message` | `set_payload` | static_content (literal JSON/string) |
| Set Properties | `documentproperties` | `set_variable` | properties list (name, source) |
| Decision | `decision` | `choice_router` | Flagged requires_review |
| Route | `route` | `choice_router` | Flagged requires_review |
| Branch | `branch` | `branch` | Flagged requires_review |
| Process Call | `processcall` | `subprocess_call` | called_process_id |
| Return Documents | `returndocuments` | (implicit end) | Not emitted as a step |
| Stop | `stop` | `exception` | |
| Notify | `notify` | `logger` | Skipped by default in generators |

---

## Connection Types

| Boomi Connector Type | Canonical Connection `type` | Notes |
|---|---|---|
| `officialboomi-X3979C-dbv2da-prod` | `db` | DatabaseV2; driver from JDBC URL |
| `officialboomi-X3979C-rest-prod` | `http` | REST connector |
| `http` | `http` | HTTP Client connector |
| `wss` | `http_listener` | Web Services Server (trigger only) |
| `salesforce` | `salesforce` | |
| `officialboomi-X3979C-events-prod` | `event_streams` | |

---

## DB SQL Format

Boomi's DatabaseV2 uses `$param_name` for named parameters:
```sql
SELECT * FROM customers WHERE status = $status ORDER BY last_name
INSERT INTO customers (first_name, last_name) VALUES ($first_name, $last_name)
UPDATE customers SET first_name = $first_name WHERE id = $id
```

The analyzer extracts:
- `table` — first table name found via `FROM`, `INTO`, or `UPDATE` regex
- `params` — list of `$param_name` values (without `$`)
- `sql` — full original SQL
- `sql_operation` — SELECT / INSERT / UPDATE / DELETE

---

## What Gets Flagged as `requires_review: true`

| Scenario | Reason |
|---|---|
| Groovy Data Process step | Logic must be manually translated to target platform |
| Decision / Route / Branch shapes | Branching logic not auto-mapped |
| Non-WSS triggers | Only WSS and scheduler triggers are auto-analyzed |
| REST connector actions | Dependent on operation XML details not always available |
| Process Call shapes | Ensure the called process is also migrated |
| Missing operation XML | Component index doesn't contain the referenced operation |

---

## Boomi Patterns → Canonical Integration Patterns

| Boomi Process Pattern | Canonical Pattern |
|---|---|
| WSS Listen → DB → Return | `request_reply_api` |
| Scheduler → DB → REST POST | `scheduled_batch` |
| Event Streams Listen → transform → DB | `event_driven` |
| SFTP Start → parse → HTTP | `file_processing` |
| Scheduler → Salesforce GET → DB UPSERT | `system_sync` |
