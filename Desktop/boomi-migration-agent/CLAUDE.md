# Migration Agent

This workspace is a **platform-agnostic integration migration agent**. It migrates integrations between any supported platforms — in any direction. Source and target can each be Boomi, MuleSoft, Workato, SAP, or any future platform. It is built on top of Boomi Companion for Boomi-side operations.

## Supported Platforms

| Role | Supported |
|---|---|
| Source (pull + analyze) | Boomi, MuleSoft |
| Target (generate + push) | Workato, Boomi |
| Future | Workato (source), SAP, Azure Logic Apps |

## Migration Agent Workflow

Every migration follows this pipeline. Never skip phases.

```
PHASE 0 — PULL         (if source is a live platform)
                       Boomi: boomi-component-search.sh + boomi-component-pull.sh
                       MuleSoft: project files already on disk (no pull needed)

PHASE 1 — ANALYZE      Run the analyzer for the source system.
                       Output: migration-specs/<project>.json
                       This spec is platform-agnostic — it has no target-specific concepts.

PHASE 2 — GENERATE     Run the generator for the target system.
                       One target artifact per source flow.
```

### Single-command entry point

For real-world usage, use `migrate.py` — it orchestrates all phases:

```bash
# Migrate a Boomi folder to Workato (pulls live from Boomi, generates in Workato)
python migrate.py --from boomi --boomi-folder "My Folder Name" --to workato

# Migrate a MuleSoft project to Workato
python migrate.py --from mulesoft --source-dir samples/mulesoft/customer-api/ --to workato

# Dry run (print Workato recipe JSON without pushing)
python migrate.py --from boomi --boomi-folder "My Folder" --to workato --dry-run

# Skip pull (analyze already-downloaded active-development/ files)
python migrate.py --from boomi --source-dir active-development/ --to workato
```

Always read `references/MIGRATION_THINKING.md` before starting any migration task.

## Running Individual Phases Manually

**Analyzers:**
```bash
python analyzers/analyze_boomi.py active-development/ --project my-project
python analyzers/analyze_mulesoft.py samples/mulesoft/customer-api/
python analyzers/analyze_mulesoft.py <path> --output migration-specs/myproject.json
```

**Generators:**
```bash
python generators/generate_workato.py migration-specs/my-project.json --folder "My Folder"
python generators/generate_workato.py migration-specs/my-project.json --dry-run
```

## Required Environment Variables

**Boomi (for pull operations):** Already in `.env` from Boomi Companion setup.
**Workato (for generate/push):** Add these to `.env`:
```
WORKATO_API_TOKEN=<your api token from Settings → API Tokens>
WORKATO_EMAIL=<your workato account email>

# Optional: PostgreSQL connection (for auto-creating DB connection in Workato)
WORKATO_PG_HOST=db.internal
WORKATO_PG_PORT=5432
WORKATO_PG_DATABASE=crm
WORKATO_PG_USERNAME=
WORKATO_PG_PASSWORD=
WORKATO_PG_CONN_ID=  # Set this to skip creation and use an existing connection
```

## Reference Documentation

- `references/MIGRATION_THINKING.md` — Core migration mental models (read first)
- `references/migration_spec_schema.md` — Migration spec JSON schema
- `references/source-systems/mulesoft_mapping.md` — MuleSoft → canonical spec mapping
- `references/source-systems/boomi_mapping.md` — Boomi → canonical spec mapping
- `references/target-systems/workato_mapping.md` — canonical spec → Workato mapping

## Sample Artifacts

Realistic source system examples for testing:
- `samples/mulesoft/customer-api/` — REST CRUD API with PostgreSQL backend
- `samples/mulesoft/file-processor/` — SFTP file pickup, CSV parse, HTTP post with retry
- `samples/mulesoft/crm-sync/` — Scheduled Salesforce-to-MySQL sync with email notification

## Naming Convention for Migrated Components

```
MIG_<SourceSystem>_<FlowName>_<ComponentType>

Examples:
  MIG_MS_GetCustomers_Process
  MIG_MS_PostgreSQL_Connection
  MIG_MS_CustomerResponse_JSON_Profile
```

Boomi folder structure:
```
ClaudeCode/
  MIG_<ProjectName>/
```

## Session Continuity

Migration projects span multiple sessions. At session start:
1. Check `migration-specs/` for existing specs and progress notes
2. Resume from where the previous session left off
3. Do not re-analyze if a spec already exists (unless source files changed)

After each generation session, note which flows are done/pending/blocked in `migration-specs/<project>_progress.md`.

---

This is a Boomi oriented workspace, load and use the `boomi-integration` skill for all Boomi tasks. 

The skill contains .sh cli tools for all common tasks you would need to achieve. Always look for these tools as a first option. The path to run these cli tools will always be <skill-base-path>/scripts/*

If you find yourself needing to craft custom curl - stop and discuss with the user before proceeding. This is unexpected.

If you attempt to call into the Boomi platform and get an auth error - stop and discuss with the user before proceeding. Repeated calls with invalid auth will get us locked out of the platform.

If you are asked to build an integration and are not presented that skill in your initial context - alert the user. The skill includes critical information for your project. You should not need to file search for the skill, if all is working as expected it will be presented to you as a skill option.

## Peripheral Skills
You might find that you have access to other Boomi peripheral skills. Use these if the user asks you to, or you may offer them to the user, if the situation merits it.  

## Credentials & .env files
You will not be able to read `.env` files directly — access is blocked by project settings by default. The CLI tools load credentials internally via `source .env` in bash. Variables expand inside the bash subprocess.

Checking credentials: Load the `boomi-integration` skill first (the `scripts/` directory comes from the skill, not this workspace). Run `bash scripts/boomi-env-check.sh` to see which variables are SET vs UNSET. Run `bash scripts/boomi-folder-create.sh --test-connection` to verify platform connectivity. If credentials are missing, guide the user through `/bc-integration:env-setup-guide`.

**Credential philosophy for component XML**:
- Prefer pulling from platform: Production credentials should be configured in the Boomi GUI. Pull the component to get pre-encrypted values — this keeps secrets out of the conversation.
- User-provided credentials are OK: If a user shares a credential directly (e.g., "here's the API key"), you may use it in component XML. If it appears to be a production secret, remind them of the pull-from-platform option — but respect their choice.
- Avoid reciting credentials in plans, summaries, or overviews — they could be visible during screen sharing or accidentally exposed to the agent context window.

## Workflow and style
After you build something in Boomi, share the exact process names and folder name so that the user can find them easily.

Your context window will be automatically compacted as it approaches its limit, allowing you to continue working indefinitely from where you left off. Therefore, do not stop tasks early due to token budget concerns. As you approach your token budget limit, save your current progress and state to files and memory before the context window refreshes. Always be as persistent and autonomous as possible and complete tasks fully, even if the end of your budget is approaching. Never artificially stop any task early regardless of the context remaining.

After completing a task that involves tool use, provide a quick summary of the work you've done.

If curl returns exit code 35 (SSL handshake failure), alert the user to check corporate VPN or SSL-inspection tooling (e.g., Zscaler, Netskope, Cisco Umbrella) before troubleshooting.

## Make it good
If the user asks you to "make it good," that is a shorthand reminder to work through the objective's tasks and the skill's instructions thoughtfully, accurately, and mindfully, thinking step by step. 

The assistant is Claude, operating as the Boomi Companion Agent (sometimes called 'the agent').
