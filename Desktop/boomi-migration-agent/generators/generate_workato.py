#!/usr/bin/env python3
"""
Workato Recipe Generator (Spec-Driven)
Reads any platform-agnostic migration spec and creates Workato recipes via the API.
Works with specs produced by analyze_mulesoft.py, analyze_boomi.py, or any future analyzer.

Usage:
    python generators/generate_workato.py migration-specs/boomi-customer-api.json
    python generators/generate_workato.py migration-specs/boomi-customer-api.json --folder MIG_CustomerAPI
    python generators/generate_workato.py migration-specs/boomi-customer-api.json --dry-run

Required environment variables (add to .env):
    WORKATO_API_TOKEN   Workato API token (Settings → API Tokens)
    WORKATO_EMAIL       Workato account email

Optional DB connection variables (for auto-creating the PostgreSQL connection):
    WORKATO_PG_HOST     PostgreSQL host       (default: db.internal)
    WORKATO_PG_PORT     PostgreSQL port       (default: 5432)
    WORKATO_PG_DATABASE PostgreSQL database   (default: from JDBC URL in spec)
    WORKATO_PG_USERNAME PostgreSQL username
    WORKATO_PG_PASSWORD PostgreSQL password
    WORKATO_PG_CONN_ID  Skip creation, use this existing Workato connection ID
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime


# ─── Workato API Client ───────────────────────────────────────────────────────

class WorkatoClient:
    BASE_URL = "https://www.workato.com/api"

    def __init__(self, api_token, email=None):
        try:
            import requests
            self._requests = requests
        except ImportError:
            print("ERROR: 'requests' library not found. Run: pip install requests", file=sys.stderr)
            sys.exit(1)

        self.api_token = api_token
        self.session = self._requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if email:
            self.session.headers.update({"x-user-email": email})

    def _get(self, path, params=None):
        r = self.session.get(f"{self.BASE_URL}{path}", params=params)
        if not r.ok:
            raise RuntimeError(f"GET {path} → {r.status_code}: {r.text[:300]}")
        return r.json()

    def _post(self, path, body):
        r = self.session.post(f"{self.BASE_URL}{path}", json=body)
        if not r.ok:
            raise RuntimeError(f"POST {path} → {r.status_code}: {r.text[:500]}")
        return r.json()

    # Folders
    def list_folders(self):
        data = self._get("/folders")
        return data.get("items", data) if isinstance(data, dict) else data

    def create_folder(self, name, parent_id=None):
        body = {"name": name}
        if parent_id:
            body["parent_id"] = str(parent_id)
        return self._post("/folders", body)

    def find_or_create_folder(self, name):
        for folder in self.list_folders():
            if folder.get("name") == name:
                return folder["id"]
        result = self.create_folder(name)
        return result["id"]

    # Connections
    def list_connections(self, folder_id=None):
        params = {"folder_id": str(folder_id)} if folder_id else {}
        data = self._get("/connections", params=params)
        return data.get("items", data) if isinstance(data, dict) else data

    def create_connection(self, name, provider, folder_id, input_cfg=None):
        body = {
            "name": name,
            "provider": provider,
            "folder_id": folder_id,
        }
        if input_cfg:
            body["input"] = input_cfg
        return self._post("/connections", body)

    def find_or_create_connection(self, name, provider, folder_id, input_cfg=None):
        for conn in self.list_connections(folder_id):
            if conn.get("name") == name:
                return conn["id"]
        result = self.create_connection(name, provider, folder_id, input_cfg)
        return result["id"]

    # Recipes
    def list_recipes(self, folder_id=None):
        params = {"folder_id": str(folder_id)} if folder_id else {}
        data = self._get("/recipes", params=params)
        return data.get("items", data) if isinstance(data, dict) else data

    def create_recipe(self, name, code, config, folder_id, description=""):
        body = {
            "recipe": {
                "name": name,
                "code": json.dumps(code) if not isinstance(code, str) else code,
                "config": json.dumps(config) if not isinstance(config, str) else config,
                "folder_id": str(folder_id),
                "description": description,
            }
        }
        return self._post("/recipes", body)

    def get_recipe(self, recipe_id):
        return self._get(f"/recipes/{recipe_id}")


# ─── Recipe Code Builders ────────────────────────────────────────────────────

def new_uuid():
    return str(uuid.uuid4())


def _step(number, provider, name, keyword, input_cfg, block=None, label=None):
    """Build a Workato recipe step dict."""
    s = {
        "number": number,
        "provider": provider,
        "name": name,
        "as": name,
        "title": label,
        "keyword": keyword,
        "dynamicPickListSelection": {},
        "toggleCfg": {},
        "input": input_cfg,
        "uuid": new_uuid(),
    }
    if block is not None:
        s["block"] = block
    return s


# ── Trigger builders ─────────────────────────────────────────────────────────

def trigger_http(http_method, path, description=""):
    """
    Workato callable recipe trigger — exposes the recipe as an HTTP endpoint.
    provider: workato, name: callable_recipe
    """
    return _step(
        number=0,
        provider="workato",
        name="callable_recipe",
        keyword="trigger",
        input_cfg={
            "http_method": http_method.lower(),
            "request_url_suffix": path,
            "response_type": "dynamic",
        },
        label=description or f"Receive {http_method.upper()} {path}",
    )


def trigger_scheduler(cron_expression="0 * * * *"):
    """Workato scheduled trigger (clock)."""
    return _step(
        number=0,
        provider="clock",
        name="scheduled_event",
        keyword="trigger",
        input_cfg={"time_unit": "hours", "trigger_every": "1"},
        label="Run on schedule",
    )


# ── Action builders ──────────────────────────────────────────────────────────

def action_pg_search(number, table, where_clause="", where_params=None, label="Search rows"):
    """PostgreSQL search_rows action."""
    input_cfg = {"schema_table_name": f"public.{table}"}
    if where_clause:
        input_cfg["where_clause"] = where_clause
    if where_params:
        input_cfg["input"] = [
            {"name": k, "type": "string", "value": v} for k, v in where_params.items()
        ]
    return _step(number, "postgresql", "search_rows", "action", input_cfg, label=label)


def action_pg_insert(number, table, field_map, label="Insert row"):
    """PostgreSQL insert_row action."""
    return _step(number, "postgresql", "insert_row", "action", {
        "schema_table_name": f"public.{table}",
        "input": [{"name": k, "value": v} for k, v in field_map.items()],
    }, label=label)


def action_pg_update(number, table, field_map, where_clause="", where_params=None, label="Update rows"):
    """PostgreSQL update_rows action."""
    input_cfg = {
        "schema_table_name": f"public.{table}",
        "input": [{"name": k, "value": v} for k, v in field_map.items()],
    }
    if where_clause:
        input_cfg["where_clause"] = where_clause
    if where_params:
        input_cfg["where_input"] = [
            {"name": k, "type": "string", "value": v} for k, v in where_params.items()
        ]
    return _step(number, "postgresql", "update_rows", "action", input_cfg, label=label)


def action_http_response(number, body_expr, status_code="200", label="Return response"):
    """Callable recipe HTTP response action."""
    return _step(number, "workato", "callable_recipe_response", "action", {
        "response_body": body_expr,
        "response_status_code": str(status_code),
        "reply_content_type": "application/json",
    }, label=label)


def action_if(number, operand, operator, value, true_block, label="If condition"):
    """Workato IF condition action."""
    return _step(number, "workato", "if_condition", "action", {
        "operand": operand,
        "operator": operator,
        "value": value,
    }, block=true_block, label=label)


def action_else(number, block, label="Else"):
    """Workato ELSE action (follows IF)."""
    return _step(number, "workato", "else_condition", "action", {}, block=block, label=label)


def action_note(number, message, label="Note"):
    """Placeholder step for logic that requires manual implementation."""
    return _step(number, "workato", "custom_action", "action", {
        "note": message,
    }, label=f"[REVIEW REQUIRED] {label}")


# ─── Spec → Workato Recipe Translator ────────────────────────────────────────

class RecipeBuilder:
    """
    Translates a single flow from the migration spec into a Workato recipe.
    Handles: HTTP triggers, DB CRUD, static responses, transforms, Groovy scripts.
    """

    def __init__(self, flow, spec):
        self.flow = flow
        self.spec = spec
        self.step_counter = 0
        self.notes = []  # Items flagged for manual review

    def next_num(self):
        self.step_counter += 1
        return self.step_counter

    def build(self):
        """Build and return (trigger_with_block, config_list, notes)."""
        trigger = self._build_trigger()
        if trigger is None:
            return None, [], ["No trigger built — flow trigger type not supported"]

        steps = self._build_steps()
        trigger["block"] = steps

        config = self._build_config()
        return trigger, config, self.notes

    def _build_trigger(self):
        t = self.flow.get("trigger", {})
        ttype = t.get("type", "")

        if ttype == "http_listener":
            method = t.get("http_method", "GET")
            path = t.get("rest_path") or t.get("path", "/endpoint")
            description = t.get("label", "")
            return trigger_http(method, path, description)

        if ttype in ("scheduler", "scheduled"):
            return trigger_scheduler()

        self.notes.append(f"Trigger type '{ttype}' not automatically mapped — created placeholder")
        return trigger_http("GET", "/migrated-endpoint", f"Placeholder trigger for '{self.flow['name']}'")

    def _build_steps(self):
        blocks = []
        steps = self.flow.get("steps", [])

        for step in steps:
            stype = step.get("type", "")
            num = self.next_num()

            if stype == "db_select":
                blocks.append(self._db_select(num, step))

            elif stype == "db_insert":
                blocks.append(self._db_insert(num, step))

            elif stype == "db_update":
                blocks.append(self._db_update(num, step))

            elif stype == "db_delete":
                note = f"DB DELETE on table {step.get('table')} — implement as Workato delete_rows action"
                self.notes.append(note)
                blocks.append(action_note(num, note, label=step.get("label", "DB Delete")))

            elif stype == "set_payload":
                # Static response — emit as final callable_recipe_response
                content = step.get("static_content", "")
                blocks.append(action_http_response(num, content, "200", step.get("label", "Return response")))

            elif stype == "transform":
                note = (
                    f"Boomi Map step '{step.get('label', '')}' — implement field mapping "
                    f"in Workato using formula pills or a separate recipe step"
                )
                self.notes.append(note)
                blocks.append(action_note(num, note, label=step.get("label", "Transform")))

            elif stype == "set_variable":
                # Set Properties — usually capturing a URL param into a variable
                # In Workato callable recipes, URL params are available in trigger output
                note = (
                    f"Boomi Set Properties step '{step.get('label', '')}' — "
                    f"in Workato, use trigger_input or trigger URL params directly"
                )
                self.notes.append(note)
                blocks.append(action_note(num, note, label=step.get("label", "Set Variable")))

            elif stype == "custom_script":
                # Groovy script — preserve the logic as a note
                purpose = step.get("purpose", "custom_transform")
                script = step.get("script", "")
                note = (
                    f"Boomi Groovy script (purpose: {purpose}) — "
                    f"implement equivalent logic in Workato using formula pills or custom Ruby. "
                    f"Original script:\n{script[:400]}{'...' if len(script) > 400 else ''}"
                )
                self.notes.append(note)
                blocks.append(action_note(num, note, label=step.get("label", "Custom Script")))

            elif stype == "logger":
                # Skip loggers — Workato has built-in logging
                pass

            elif stype == "return_response":
                # Already handled via set_payload above
                pass

            else:
                note = f"Step type '{stype}' not mapped — '{step.get('label', '')}'"
                self.notes.append(note)
                blocks.append(action_note(num, note, label=step.get("label", stype)))

        # If no response step was added, add a default 200 OK
        if blocks and not any(
            s.get("name") in ("callable_recipe_response",) for s in blocks
        ):
            blocks.append(action_http_response(self.next_num(), '{"status": "ok"}', "200", "Return response"))

        return blocks

    def _db_select(self, number, step):
        sql = step.get("sql", "")
        table = step.get("table", "table")
        params = step.get("params", [])
        label = step.get("label", f"Search {table}")

        # Build Workato where clause from SQL params
        where_clause, where_params = self._sql_to_workato_where(sql, params)

        return action_pg_search(
            number=number,
            table=table,
            where_clause=where_clause,
            where_params=where_params,
            label=label,
        )

    def _db_insert(self, number, step):
        sql = step.get("sql", "")
        table = step.get("table", "table")
        params = step.get("params", [])
        label = step.get("label", f"Insert into {table}")

        # Map $param_name → trigger input pill
        field_map = {p: self._param_to_workato_ref(p) for p in params}
        return action_pg_insert(number, table, field_map, label=label)

    def _db_update(self, number, step):
        sql = step.get("sql", "")
        table = step.get("table", "table")
        params = step.get("params", [])
        label = step.get("label", f"Update {table}")

        # For UPDATE: separate SET params from WHERE params
        # Heuristic: "id" param is a WHERE param, rest are SET params
        where_params_list = [p for p in params if p.lower() in ("id", "key")]
        set_params_list = [p for p in params if p not in where_params_list]

        # If no obvious WHERE param found, use all as SET and note it
        if not where_params_list and params:
            where_params_list = [params[-1]]
            set_params_list = params[:-1]

        field_map = {p: self._param_to_workato_ref(p) for p in set_params_list}
        where_clause = " AND ".join(f'{p} = "{{{{input.{p}}}}}"' for p in where_params_list)
        where_params = {p: self._param_to_workato_ref(p) for p in where_params_list}

        return action_pg_update(number, table, field_map, where_clause, where_params, label=label)

    def _sql_to_workato_where(self, sql, params):
        """
        Convert a Boomi SQL WHERE clause ($param notation) to Workato format.
        Returns (where_clause_str, where_params_dict).
        """
        if not sql or not params:
            return "", {}

        # Extract WHERE clause
        m = re.search(r"WHERE\s+(.+?)(?:ORDER BY|GROUP BY|LIMIT|$)", sql, re.IGNORECASE | re.DOTALL)
        if not m:
            return "", {p: self._param_to_workato_ref(p) for p in params}

        where_raw = m.group(1).strip()
        # Replace $param with Workato pill syntax for the where_clause display
        where_display = re.sub(r"\$(\w+)", lambda m: f'"{{{{input.{m.group(1)}}}}}"', where_raw)

        where_params = {p: self._param_to_workato_ref(p) for p in params}
        return where_display, where_params

    def _param_to_workato_ref(self, param_name):
        """Map a Boomi SQL $param_name to a Workato formula pill."""
        # Common REST patterns: camelCase in body, snake_case in DB
        camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), param_name)
        return f"#{{trigger_input['{camel}'] || trigger_input['{param_name}']}}"

    def _build_config(self):
        """Build the config list declaring which connectors are used."""
        config = [{"keyword": "application", "name": "workato", "provider": "workato"}]
        # Check if any step uses a DB connection
        for step in self.flow.get("steps", []):
            if step.get("type", "").startswith("db_"):
                config.append({"keyword": "application", "name": "postgresql", "provider": "postgresql"})
                break
        return config


# ─── DB Connection Setup ─────────────────────────────────────────────────────

def get_pg_connection_id(client, spec, folder_id):
    """Find or create a PostgreSQL connection for this migration."""
    existing = os.environ.get("WORKATO_PG_CONN_ID")
    if existing:
        print(f"  Using existing PostgreSQL connection: {existing}")
        return existing

    # Try to extract connection info from spec
    conn_info = {}
    for conn in spec.get("connections", {}).values():
        if conn.get("type") == "db" and conn.get("driver") in ("postgresql", "postgres"):
            conn_info = conn
            break

    conn_name = f"MIG_{spec.get('project_name', 'Project')}_PostgreSQL"
    input_cfg = {
        "host": os.environ.get("WORKATO_PG_HOST", conn_info.get("host", "db.internal")),
        "port": os.environ.get("WORKATO_PG_PORT", conn_info.get("port", "5432")),
        "database": os.environ.get("WORKATO_PG_DATABASE", conn_info.get("database", "crm")),
        "username": os.environ.get("WORKATO_PG_USERNAME", ""),
        "password": os.environ.get("WORKATO_PG_PASSWORD", ""),
    }

    conn_id = client.find_or_create_connection(conn_name, "postgresql", folder_id, input_cfg)
    print(f"  PostgreSQL connection: '{conn_name}' (id: {conn_id})")
    return conn_id


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate Workato recipes from any migration spec JSON."
    )
    parser.add_argument("spec", nargs="?", default="migration-specs/boomi-customer-api.json",
                        help="Path to migration spec JSON (default: migration-specs/boomi-customer-api.json)")
    parser.add_argument("--folder", default=None,
                        help="Workato folder name (default: MIG_<project_name>)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print generated recipe JSON without calling the Workato API")
    args = parser.parse_args()

    # Load spec
    if not os.path.isfile(args.spec):
        print(f"ERROR: Spec file not found: {args.spec}", file=sys.stderr)
        sys.exit(1)

    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)

    project_name = spec.get("project_name", "MigrationProject")
    folder_name = args.folder or f"MIG_{project_name}"
    flows = spec.get("integrations", [])

    print(f"Loaded spec: {project_name}")
    print(f"  Source system : {spec.get('source_system', 'unknown')}")
    print(f"  Flows         : {len(flows)}")
    print(f"  Target folder : {folder_name}")

    # Dry run — just print
    if args.dry_run:
        print("\n=== DRY RUN ===\n")
        for flow in flows:
            builder = RecipeBuilder(flow, spec)
            code, config, notes = builder.build()
            flow_name = flow['name']
            recipe_name = flow_name if flow_name.upper().startswith("MIG_") else f"MIG_{flow_name}"
            print(f"Recipe: {recipe_name}")
            print(json.dumps(code, indent=2))
            if notes:
                print(f"\nManual review required ({len(notes)} items):")
                for n in notes:
                    print(f"  • {n[:120]}")
            print("-" * 60)
        return

    # Live run
    api_token = os.environ.get("WORKATO_API_TOKEN") or _load_env_var("WORKATO_API_TOKEN")
    email = os.environ.get("WORKATO_EMAIL") or _load_env_var("WORKATO_EMAIL")

    if not api_token:
        print("ERROR: WORKATO_API_TOKEN not set.", file=sys.stderr)
        print("  Add it to your .env file: WORKATO_API_TOKEN=your_token_here", file=sys.stderr)
        sys.exit(1)

    client = WorkatoClient(api_token, email)

    print(f"\nConnecting to Workato API...")

    # Ensure folder
    print(f"Ensuring folder '{folder_name}'...")
    folder_id = client.find_or_create_folder(folder_name)
    print(f"  Folder id: {folder_id}")

    # Ensure DB connection (if any flows use a DB)
    needs_pg = any(
        step.get("type", "").startswith("db_")
        for flow in flows
        for step in flow.get("steps", [])
    )
    pg_conn_id = None
    if needs_pg:
        print(f"\nEnsuring PostgreSQL connection...")
        pg_conn_id = get_pg_connection_id(client, spec, folder_id)

    # Generate and push recipes
    print(f"\nCreating {len(flows)} recipes...")
    results = []
    all_notes = []

    for flow in flows:
        flow_name = flow['name']
        # Avoid double MIG_ prefix if the source name already starts with MIG_
        if flow_name.upper().startswith("MIG_"):
            recipe_name = flow_name
        else:
            recipe_name = f"MIG_{flow_name}"
        print(f"  Creating '{recipe_name}'...")

        builder = RecipeBuilder(flow, spec)
        code, config, notes = builder.build()
        all_notes.extend([(recipe_name, n) for n in notes])

        if code is None:
            print(f"    SKIP: no recipe built")
            results.append({"name": recipe_name, "status": "skipped"})
            continue

        description = (
            f"Migrated from {spec.get('source_system', 'source')} by migration-agent. "
            f"Source flow: {flow['name']}. Generated: {datetime.utcnow().isoformat()}Z"
        )

        try:
            result = client.create_recipe(recipe_name, code, config, folder_id, description)
            recipe_id = result.get("id")
            print(f"    Created (id: {recipe_id})")
            results.append({"name": recipe_name, "id": recipe_id, "status": "created", "manual_review_items": len(notes)})
            time.sleep(0.3)
        except Exception as e:
            print(f"    FAILED: {e}", file=sys.stderr)
            results.append({"name": recipe_name, "status": "failed", "error": str(e)})

    # Save output
    output = {
        "project": project_name,
        "folder_id": folder_id,
        "folder_name": folder_name,
        "pg_connection_id": pg_conn_id,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "recipes": results,
        "manual_review_required": [
            {"recipe": name, "note": note} for name, note in all_notes
        ],
    }

    output_path = args.spec.replace(".json", "_workato_output.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'=' * 50}")
    created = sum(1 for r in results if r["status"] == "created")
    print(f"Done. {created}/{len(flows)} recipes created.")
    print(f"Output saved to: {output_path}")

    if all_notes:
        print(f"\n[!] {len(all_notes)} items require manual review in Workato:")
        for recipe_name, note in all_notes[:10]:
            print(f"   [{recipe_name}] {note[:100]}")
        if len(all_notes) > 10:
            print(f"   ... and {len(all_notes) - 10} more (see output file)")


def _load_env_var(key):
    """Try to load a variable from .env file (fallback if not in environment)."""
    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        return None
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


if __name__ == "__main__":
    main()
