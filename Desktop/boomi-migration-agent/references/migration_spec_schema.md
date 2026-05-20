# Migration Spec Schema

The migration spec is the normalized intermediate format produced by all source system analyzers. It is the contract between the analysis phase and the Boomi generation phase.

**File location**: `migration-specs/<project-name>.json`

---

## Top-Level Structure

```json
{
  "schema_version": "1.0",
  "source_system": "mulesoft",
  "source_version": "4.x",
  "analyzed_at": "2026-01-01T00:00:00Z",
  "project_name": "customer-api",
  "connections": { ... },
  "integrations": [ ... ],
  "gaps": [ ... ],
  "migration_notes": ""
}
```

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Spec schema version (always "1.0" currently) |
| `source_system` | string | `mulesoft` \| `sap` \| `database` \| `flatfile` \| `generic` |
| `source_version` | string | Source platform version, e.g. `"4.x"`, `"3.9"` |
| `analyzed_at` | ISO 8601 | Timestamp of analysis run |
| `project_name` | string | Name of the source project/application |
| `connections` | object | Keyed by config name; all global connection configs |
| `integrations` | array | One entry per flow/sub-flow |
| `gaps` | array | Components that couldn't be cleanly mapped |
| `migration_notes` | string | Free-text notes from analyzer or human reviewer |

---

## `connections` Object

Keyed by the source system's config reference name.

```json
{
  "PostgreSQL_Config": {
    "type": "db",
    "driver": "postgresql",
    "host": "db.internal",
    "port": "5432",
    "database": "crm",
    "uses_env_vars": true,
    "boomi_equivalent": "databasev2_connection",
    "boomi_driver": "PostgreSQL",
    "notes": "Credentials in ${db.user}/${db.password}"
  },
  "HTTP_Listener_Config": {
    "type": "http_listener",
    "host": "0.0.0.0",
    "port": "8081",
    "boomi_equivalent": "wss_listener",
    "notes": "Check atom API tier before choosing WSS vs API Service Component"
  }
}
```

### Connection `type` Values

| Type | Source Example | Boomi Equivalent |
|---|---|---|
| `http_listener` | MuleSoft `http:listener-config` | WSS operation |
| `http_request` | MuleSoft `http:request-config` | REST connection |
| `db` | MuleSoft `db:config` | DatabaseV2 connection |
| `sftp` | MuleSoft `sftp:config` | Disk V2 connection (SFTP) |
| `file` | MuleSoft `file:config` | Disk V2 connection (local) |
| `salesforce` | MuleSoft `salesforce:sfdc-config` | Salesforce connection (pull from platform) |
| `email_smtp` | MuleSoft `email:smtp-config` | Mail connector |
| `jms` | MuleSoft `jms:config` | Event Streams or JMS connector |
| `custom` | Unknown | Requires review |

---

## `integrations` Array

Each entry represents one flow or sub-flow.

```json
{
  "name": "get-customers-flow",
  "source_name": "get-customers-flow",
  "flow_type": "primary",
  "trigger": { ... },
  "steps": [ ... ],
  "error_handling": { ... },
  "boomi_suggestions": { ... }
}
```

| Field | Type | Description |
|---|---|---|
| `name` | string | Normalized name (kebab-case) |
| `source_name` | string | Original name in source system |
| `flow_type` | string | `primary` \| `sub_flow` \| `error_handler` |
| `trigger` | object | What starts this flow (null for sub-flows) |
| `steps` | array | Ordered processing steps |
| `error_handling` | object | Error handling configuration |
| `boomi_suggestions` | object | Recommended Boomi implementation |

### `trigger` Object

```json
{
  "type": "http_listener",
  "config_ref": "HTTP_Listener_Config",
  "path": "/api/customers",
  "method": "GET",
  "allowed_methods": ["GET"]
}
```

**Trigger `type` values:**

| Type | Description | Boomi Start Shape |
|---|---|---|
| `http_listener` | HTTP/HTTPS inbound endpoint | WSS Listener |
| `scheduler_cron` | Cron-based schedule | Schedule (Cron) |
| `scheduler_fixed` | Fixed-interval schedule | Schedule (Fixed Interval) |
| `file_listener` | File system polling | Disk V2 Listen |
| `sftp_listener` | SFTP polling | Disk V2 Listen (SFTP) |
| `jms_listener` | JMS queue/topic listener | Event Streams Listen or JMS |
| `sub_flow` | Called by another flow (no trigger) | No start shape (subprocess) |

### `steps` Array

Each step in order of execution.

```json
{
  "sequence": 1,
  "type": "db_select",
  "label": "Query Customers",
  "config_ref": "PostgreSQL_Config",
  "sql": "SELECT * FROM customers WHERE status = :status",
  "has_parameters": true,
  "parameters": ["status"],
  "boomi_step": "DatabaseV2_Connector",
  "boomi_operation_type": "GET",
  "requires_review": false
}
```

**Step `type` values:**

| Type | Description | Boomi Step |
|---|---|---|
| `set_variable` | Set a named variable | Set Properties (DPP) |
| `set_payload` | Replace payload content | Message step |
| `transform` | DataWeave transformation | Map component |
| `logger` | Log a message | Notify step |
| `flow_ref` | Call another flow | Process Call step |
| `db_select` | Database SELECT | DatabaseV2 GET operation |
| `db_insert` | Database INSERT | DatabaseV2 INSERT operation |
| `db_update` | Database UPDATE | DatabaseV2 UPDATE operation |
| `db_delete` | Database DELETE | DatabaseV2 DELETE operation |
| `db_stored_procedure` | Stored procedure call | DatabaseV2 Stored Procedure operation |
| `http_request` | Outbound HTTP call | REST Connector step |
| `choice_router` | Conditional branch (2-path) | Decision step |
| `choice_router_multi` | Conditional branch (3+ paths) | Route step |
| `scatter_gather` | Parallel paths | Branch step (note: Boomi is sequential) |
| `foreach` | Loop over collection | Data Process (Split) |
| `try_scope` | Error handling scope | Try/Catch step |
| `async_scope` | Asynchronous execution | Separate process (flag gap) |
| `batch_job` | Batch processing | Data Process split + subprocess |
| `file_read` | Read from file system | Disk V2 GET operation |
| `file_write` | Write to file system | Disk V2 CREATE/UPSERT operation |
| `file_list` | List files | Disk V2 QUERY operation |
| `file_delete` | Delete file | Disk V2 DELETE operation |
| `sftp_read` | SFTP file read | Disk V2 GET (SFTP connection) |
| `sftp_write` | SFTP file write | Disk V2 CREATE (SFTP connection) |
| `salesforce_query` | Salesforce SOQL query | Salesforce Query operation |
| `salesforce_create` | Salesforce record create | Salesforce Create operation |
| `salesforce_update` | Salesforce record update | Salesforce Update operation |
| `salesforce_upsert` | Salesforce record upsert | Salesforce Upsert operation |
| `email_send` | Send email | Mail connector (notify pattern) |
| `jms_publish` | Publish to JMS | Event Streams Produce operation |
| `raise_error` | Throw an exception | Exception step |
| `custom` | Unknown/unrecognized | REVIEW REQUIRED |

### `error_handling` Object

```json
{
  "has_error_handler": true,
  "strategies": [
    {
      "error_type": "DB:CONNECTIVITY",
      "strategy": "propagate",
      "boomi_equivalent": "try_catch_rethrow"
    },
    {
      "error_type": "ANY",
      "strategy": "propagate",
      "boomi_equivalent": "try_catch_rethrow"
    }
  ]
}
```

| Strategy | Description | Boomi Pattern |
|---|---|---|
| `propagate` | Error bubbles up to caller | Try/Catch → Exception step in catch |
| `continue` | Error is swallowed, flow continues | Try/Catch → Notify in catch, no Exception |

### `boomi_suggestions` Object

Recommendations generated by the analyzer based on the mapping table.

```json
{
  "process_name": "MIG_MS_GetCustomers_Process",
  "pattern": "request_reply_api",
  "trigger_component": "WSS_Listener",
  "step_components": [
    "Set_Properties",
    "DatabaseV2_Connector_GET",
    "Map_Transform",
    "Notify"
  ],
  "connections_needed": ["databasev2_connection"],
  "complexity": "low",
  "manual_review_required": false,
  "notes": ""
}
```

**`pattern` values:**

| Pattern | Description |
|---|---|
| `request_reply_api` | HTTP listener → process → return response |
| `scheduled_batch` | Scheduler → fetch → transform → deliver |
| `file_processing` | File listener → parse → transform → deliver |
| `event_driven` | Message listener → process |
| `system_sync` | Scheduler → query source → upsert target |
| `subprocess` | No trigger, called by parent process |

**`complexity` values:** `low` | `medium` | `high`
- `low`: Straightforward mapping, no gaps
- `medium`: Some DataWeave complexity or non-obvious mapping
- `high`: Gaps present, parallel execution, batch logic, or complex transformations

---

## `gaps` Array

```json
[
  {
    "flow_name": "sftp-order-pickup-flow",
    "step_sequence": 3,
    "source_type": "scatter_gather",
    "issue": "MuleSoft scatter-gather executes routes in parallel. Boomi Branch step is sequential.",
    "resolution": "Implemented as Boomi Branch (sequential). Parallelism not preserved.",
    "severity": "low"
  }
]
```

| Severity | Meaning |
|---|---|
| `low` | Behavioral difference is minor or unlikely to matter |
| `medium` | Behavioral difference could affect performance or correctness in edge cases |
| `high` | Significant behavioral difference; requires user decision before migration |
| `blocked` | Cannot migrate without more information; migration halted for this flow |
