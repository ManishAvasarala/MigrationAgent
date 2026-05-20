# MuleSoft → Boomi Component Mapping Reference

## Quick Reference: Connector Types

| MuleSoft Connector | MuleSoft XML Tag | Boomi Equivalent | Boomi connectorType |
|---|---|---|---|
| HTTP Listener | `http:listener` | WSS Start shape | `wss` |
| HTTP Request | `http:request` | REST Connector step | `officialboomi-X3979C-rest-prod` |
| Database | `db:select/insert/update/delete` | Database V2 Connector | `officialboomi-X3979C-dbv2da-prod` |
| File | `file:read/write/list/delete` | Disk V2 Connector | `disk-sdk` |
| SFTP | `sftp:read/write/list` | Disk V2 Connector (SFTP) | `disk-sdk` |
| Salesforce | `salesforce:query/create/update` | Salesforce Connector | `salesforce` |
| Email SMTP | `email:send` | Mail (REST/HTTP to mail service) | varies |
| JMS | `jms:publish/consume` | Event Streams or JMS Connector | `officialboomi-X3979C-events-prod` |
| VM (in-memory queue) | `vm:publish/consume` | Event Streams (Boomi-native) | `officialboomi-X3979C-events-prod` |
| Anypoint MQ | `anypoint-mq:publish` | Event Streams | `officialboomi-X3979C-events-prod` |
| Kafka | `kafka:publish/consume` | Custom Connector or Event Streams | varies |
| Amazon SQS | `sqs:send/receive` | HTTP REST calls to SQS API | `officialboomi-X3979C-rest-prod` |
| FTP | `ftp:read/write` | Disk V2 Connector (FTP mode) | `disk-sdk` |
| Object Store | `os:store/retrieve` | Document Cache | built-in |
| Scheduler | `scheduler` | Start step (scheduled) | built-in |

---

## Trigger Mapping

### HTTP Listener → WSS Listener

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<http:listener-config             WSS operation (connector-action)
  name="HTTP_Config">             + Start shape with Listen type
  <http:listener-connection       Check atom apiType before deciding:
    host="0.0.0.0" port="8081"/>    basic/intermediate → bare WSS
</http:listener-config>             advanced → API Service Component

<http:listener                    Start shape:
  config-ref="HTTP_Config"          shapetype="start"
  path="/api/customers"             - path maps to WSS operation path
  allowedMethods="GET"/>            - method maps to WSS HTTP Method
```

**Notes:**
- Run `boomi-shared-server-info.sh` before building any WSS listener to determine API tier
- `attributes.queryParams.*` → extracted via Set Properties using `Inbound Properties`
- `attributes.uriParams.*` → extracted via Set Properties using URI path variables
- `attributes.headers.*` → extracted via Set Properties using `Inbound Properties`

### Scheduler → Start Shape (Schedule)

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<scheduler>                       Start shape with schedule type
  <scheduling-strategy>
    <cron expression="0 0 2 * * ?" timeZone="America/New_York"/>
  </scheduling-strategy>          scheduletype="advanced" (cron)
</scheduler>                      or scheduletype="delay" (fixed)
```

### SFTP/File Listener → Disk V2 Listen

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<sftp:listener                    Disk V2 connection + Listen operation
  config-ref="SFTP_Config"          - directory maps to operation path
  directory="/inbound"              - filenamePattern maps to filter
  doc:name="SFTP Listener">
  <scheduling-strategy>
    <fixed-frequency frequency="5" timeUnit="MINUTES"/>
  </scheduling-strategy>
  <sftp:matcher                     Operation filter pattern
    filenamePattern="orders_*.csv"/>
  <sftp:post-processing-action      Post-processing: move or delete
    moveToDirectory="/processed"/>    Configure in Disk V2 operation
</sftp:listener>
```

---

## Step Mapping

### DataWeave Transform → Boomi Map

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<ee:transform>                    Map component (transform.map)
  <ee:message>                      Source profile → Target profile
    <ee:set-payload>
      %dw 2.0                     Simple field mapping → Map functions
      output application/json     Conditionals → Map conditional functions
      ---                         Type coercion → Map type conversion functions
      payload map (item) -> {     Array iteration → Map with array handling
        id: item.id,              Complex reduce/recursion → Groovy script
        name: item.name
      }
    </ee:set-payload>
  </ee:message>
  <ee:variables>                  Variables set in transform →
    <ee:set-variable              Set Properties step BEFORE the Map step
      variableName="status">      (Map step only transforms the document)
      200
    </ee:set-variable>
  </ee:variables>
</ee:transform>
```

**DataWeave → Boomi Map Function Equivalents:**

| DataWeave | Boomi Map Function |
|---|---|
| `upper($)` | `StringUpper` |
| `lower($)` | `StringLower` |
| `trim($)` | `StringTrim` |
| `sizeOf($)` | `StringLength` / `Count` |
| `now()` | `CurrentDate`, `CurrentDateTime` |
| `$ as String` | `ToString` |
| `$ as Number` | `ToNumber` |
| `$ as Date` | `ToDate` |
| `payload.field default "val"` | `DefaultValue` function |
| `if (condition) a else b` | `Conditional` function |
| `payload splitBy ","` | `StringSplit` |
| `payload joinBy ","` | `StringJoin` |
| `payload filter ($ != null)` | Map with conditional — or Groovy |
| `payload map (item) -> ...` | Map component (array source profile) |

### set-variable → Set Properties (DPP)

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<set-variable                     Set Properties step
  variableName="statusFilter"       - Property type: Dynamic Process Property
  value="#[attributes.            - Name: "statusFilter"
    queryParams.status              - Value: use parameter source type
    default 'ACTIVE']"/>              "Connector" (for inbound HTTP params)
                                      or "Current Data" for payload fields
```

**MuleSoft variable sources → Boomi Set Properties sources:**

| MuleSoft Source | Boomi Parameter Source |
|---|---|
| `attributes.queryParams.*` | Connector (inbound property) |
| `attributes.uriParams.*` | Connector (inbound property) |
| `attributes.headers.*` | Connector (inbound property) |
| `payload.field` | Current Data (with XPath/JSON path) |
| Literal value | Static |
| `vars.existingVar` | Dynamic Process Property |
| `now()` | Current Date / Current Datetime |

### choice → Decision / Route

```
MuleSoft (2 paths)               Boomi
─────────────────────────────     ──────────────────────────────────
<choice>                          Decision step
  <when expression="#[           shapetype="decision"
    sizeOf(payload) == 0]">
    <!-- not found path -->         True branch → not found path
  </when>
  <otherwise>                       False branch → found path
    <!-- found path -->
  </otherwise>
</choice>

MuleSoft (3+ paths)              Boomi
<choice>                          Route step
  <when expression="...">         shapetype="route"
  <when expression="...">         One route per <when> expression
  <otherwise>                     Default route for <otherwise>
```

### foreach → Data Process (Split)

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<foreach                          Data Process step
  collection="#[payload]"           - Step type: Split Documents
  batchSize="50">                   - Combine: false
  <!-- process each item -->        Process each split document downstream
</foreach>                          batchSize maps to Split size (if supported)
```

**Note:** In Boomi, after a Data Process split, each sub-document flows through the remaining process steps independently. This is different from MuleSoft's foreach which executes the scope body per item. Structure accordingly.

### try → Try/Catch

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<try>                             Try/Catch step
  <!-- main logic -->               - Main path: main logic
  <error-handler>
    <on-error-propagate           Catch branch:
      type="DB:CONNECTIVITY">       - Exception step (rethrow)
      <!-- error logic -->
    </on-error-propagate>

    <on-error-continue            Catch branch:
      type="ANY">                   - Notify step (log only, continue)
      <!-- error logic -->           - No Exception step = flow continues
    </on-error-continue>
  </error-handler>
</try>
```

### flow-ref → Process Call

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<flow-ref                         Process Call step
  name="parse-csv-sub-flow"/>       - References the sub-flow process
                                    - Data passes as document payload
                                    - DPPs are shared (same process scope)
```

**Critical:** MuleSoft sub-flows share variable scope with the calling flow. Boomi subprocesses do NOT share DPPs by default — pass needed data as document payload or use DDPs.

### scatter-gather → Branch (with gap note)

```
MuleSoft                          Boomi (with behavioral difference)
─────────────────────────────     ──────────────────────────────────
<scatter-gather>                  Branch step
  <route>...</route>                - Path 1
  <route>...</route>                - Path 2
</scatter-gather>                   NOTE: Boomi Branch is sequential,
                                    not parallel. Add shape note.
```

**Gap:** MuleSoft scatter-gather executes routes in parallel and collects all results. Boomi Branch processes routes sequentially and routes each document down one path. This is a behavioral difference — flag it.

### logger → Notify

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<logger                           Notify step
  level="INFO"                      - Notification type: Information
  message="Processing #[           - Message: replicate template
    vars.fileName]"/>                 using {1}, {2} placeholders
                                    - Parameter sources for variables
```

Map only ERROR and WARN loggers by default. DEBUG loggers can be omitted or added selectively.

### raise-error → Exception

```
MuleSoft                          Boomi
─────────────────────────────     ──────────────────────────────────
<raise-error                      Exception step
  type="APP:VALIDATION_FAILED"      - Error message maps to description
  description="Invalid order"/>
```

---

## Connection Config Mapping

### DB Config → Database V2 Connection

| MuleSoft `db:config` | Boomi Database V2 Connection |
|---|---|
| `url="jdbc:postgresql://..."` | Connection type: PostgreSQL |
| `url="jdbc:mysql://..."` | Connection type: MySQL |
| `url="jdbc:sqlserver://..."` | Connection type: Microsoft SQL Server |
| `url="jdbc:oracle:thin:..."` | Connection type: Oracle |
| `driverClassName` (generic) | Connection type: Other; provide driver |
| `user` | User name |
| `password` | Password (enter directly or use extensions) |

### HTTP Request Config → REST Connection

| MuleSoft `http:request-config` | Boomi REST Connection |
|---|---|
| `host` + `port` + `basePath` | Base URL |
| `protocol="HTTPS"` | Connection SSL = true |
| `<http:basic-authentication>` | Authentication type: Basic |
| `<http:bearer-token-authentication>` | Auth type: Custom Header — `Authorization: Bearer {token}` |
| `<http:oauth-client-credentials>` | Auth type: OAuth 2.0 Client Credentials |
| Connection pooling settings | Connection pooling in REST connection |

### Salesforce Config → Salesforce Connection

MuleSoft Salesforce connections use username/password/token authentication. In Boomi:
- **Preferred**: Pull the existing Salesforce connection from the platform (if one exists)
- **If creating new**: Use Salesforce connector with OAuth 2.0 — requires GUI setup for initial OAuth flow
- Flag as `requires_gui_setup: true` in the migration spec if no existing connection found

### SFTP Config → Disk V2 Connection

| MuleSoft `sftp:config` | Boomi Disk V2 Connection |
|---|---|
| `host` | SFTP host |
| `port` | Port (default 22) |
| `username` | Username |
| `password` | Password |
| Private key auth | Key file — requires GUI or process extension |

---

## Pattern-Level Examples

### Pattern: Request-Reply REST API with DB Backend

**MuleSoft structure:**
```
http:listener → db:select → ee:transform → response
```

**Boomi structure:**
```
Start (WSS Listen) → Set Properties (extract params) → DatabaseV2 GET 
→ Map (result to JSON) → Return Documents
```

Required Boomi components:
1. WSS operation (connector-action)
2. DatabaseV2 connection (connector-settings)
3. DatabaseV2 GET operation (connector-action)
4. JSON Profile — Response (profile.json)
5. Map component (transform.map) if transformation needed
6. Process (process)

### Pattern: Scheduled Salesforce-to-DB Sync

**MuleSoft structure:**
```
scheduler → salesforce:query → foreach → choice(insert/update) → db:insert or db:update → email:send
```

**Boomi structure:**
```
Start (Schedule) → Salesforce Query → Data Process (Split) 
→ [Branch: 
     Path A: DatabaseV2 Check → Decision → DatabaseV2 INSERT
     Path B: DatabaseV2 UPDATE
  ]
→ Process Call (notification subprocess)
```

### Pattern: SFTP File Processing

**MuleSoft structure:**
```
sftp:listener → [parse CSV in sub-flow] → foreach → http:request → file:write (error)
```

**Boomi structure:**
```
Start (Disk V2 Listen — SFTP) → Map (CSV to profile) → Data Process (Split records)
→ Try/Catch [
     REST Connector (POST each record)
     Catch: Disk V2 Write (error file)
  ]
```

---

## Unsupported / Requires Manual Action

| MuleSoft Feature | Status | Action |
|---|---|---|
| DataWeave `dw::core::*` modules | Partial | Simple functions → Map; complex → Groovy |
| `scatter-gather` true parallelism | Gap | Use Branch (sequential), add note |
| `batch:job` record-level isolation | Gap | Data Process split + subprocess Try/Catch |
| `async` scope | Gap | Separate Boomi process |
| `until-successful` retry scope | Gap | Boomi atom-level retry or Groovy retry loop |
| OAuth user-grant flows | Requires GUI | Flag; user sets up in Boomi GUI |
| `object-store` (persistent) | No direct match | Boomi Document Cache (in-memory) or DB |
| Mule expressions in config files | N/A | Use Boomi process extensions |
| Custom Java components | Requires review | Data Process Groovy (if JVM-callable logic is simple) |
| `apikit:router` (RAML-driven routing) | Partial | One Boomi process per RAML resource+method |
