#!/usr/bin/env python3
"""
Workato Recipe Analyzer
Pulls recipes from a Workato account (or reads exported JSON) and produces
a platform-agnostic migration spec.

Usage:
    # Pull live from Workato account
    python analyzers/analyze_workato.py --folder "My Folder" --project my-project
    python analyzers/analyze_workato.py --folder-id 123456 --project my-project

    # Analyze exported recipe JSON files
    python analyzers/analyze_workato.py --source-dir path/to/exported/recipes/ --project my-project

Required environment variables (for live pull):
    WORKATO_API_TOKEN   Workato API token
    WORKATO_EMAIL       Workato account email
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


WORKATO_BASE_URL = "https://www.workato.com/api"

# Region-specific base URLs derived from token prefix
_WORKATO_REGION_URLS = {
    "wrkaus": "https://app.au.workato.com/api",
    "wrkeu":  "https://app.eu.workato.com/api",
    "wrksg":  "https://app.sg.workato.com/api",
    "wrkjp":  "https://app.jp.workato.com/api",
}

def _base_url_for_token(token: str) -> str:
    # Allow explicit override — useful when token prefix doesn't match actual datacenter
    override = os.environ.get("WORKATO_BASE_URL", "").strip()
    if override:
        return override
    prefix = token.split("-")[0].lower()
    return _WORKATO_REGION_URLS.get(prefix, WORKATO_BASE_URL)

# Workato trigger name → canonical trigger type
TRIGGER_TYPE_MAP = {
    "callable_recipe":          "http_listener",
    "webhook_event":            "http_listener",
    "receive_http_request":     "http_listener",
    "execute":                  "http_listener",   # workato_recipe_function:execute
    "scheduled_event":          "scheduler",
    "new_record":               "db_trigger",
    "updated_record":           "db_trigger",
    "new_message":              "event_trigger",
    "new_event":                "event_trigger",
    # Connector-based event triggers — Boomi equivalent is scheduled polling
    "new_spreadsheet_row_v4":   "connector_trigger",
    "new_spreadsheet_row":      "connector_trigger",
    "new_updated_row":          "connector_trigger",
    "new_file":                 "connector_trigger",
    "new_row":                  "connector_trigger",
    "new_attachment":           "connector_trigger",
    "new_lead":                 "connector_trigger",
    "new_contact":              "connector_trigger",
    "new_object":               "connector_trigger",
    "updated_object":           "connector_trigger",
}

# Fallback name-only action map (used when provider is unknown)
_NAME_ACTION_MAP = {
    "search_rows":              "db_select",
    "select_rows":              "db_select",
    "insert_row":               "db_insert",
    "create_record":            "db_insert",
    "update_rows":              "db_update",
    "update_record":            "db_update",
    "delete_rows":              "db_delete",
    "request":                  "http_request",
    "get":                      "http_request",
    "post":                     "http_request",
    "put":                      "http_request",
    "callable_recipe_response": "return_response",
    "return_response":          "return_response",
    "repeat_while":             "loop",
    "map_list":                 "transform",
    "custom_action":            "custom_script",
}


# ─── Provider-aware classification ───────────────────────────────────────────

def classify_step(provider, name, keyword):
    """
    Classify a Workato step into a canonical type using provider + name + keyword.
    Provider takes priority over name-only lookup so that e.g. salesforce:upsert
    becomes salesforce_action rather than a generic connector_action.
    """
    p = (provider or "").lower()
    n = (name or "").lower()
    kw = (keyword or "").lower()

    # Control-flow keywords take priority
    if kw == "if":
        return "choice_router"
    if kw in ("else", "elsif"):
        return "choice_router_else"
    if kw in ("foreach", "repeat", "while"):
        return "loop"
    if kw == "monitor":
        return "try_catch"

    # Provider-specific mapping
    if p == "salesforce":
        return "salesforce_action"

    if p in ("google_sheets", "googlesheets", "gsheet"):
        return "google_sheets_action"

    if p in ("workato_recipe_functions", "recipe_functions"):
        if n in ("create_list", "append_list", "clear_list", "add_list_item"):
            return "set_variable"
        return "set_variable"

    if p in ("logger", "log", "workato_logger"):
        return "log_message"

    if p == "monitor":
        return "try_catch"

    if p in ("email", "mail", "smtp"):
        return "send_email"

    if p in ("slack", "teams", "notification"):
        return "log_message"

    # Utility connector — maps to Boomi file / data process operations
    if p == "utility":
        if "read_file" in n or n == "read_file":
            return "file_read"
        if "write_file" in n or n == "write_file":
            return "file_write"
        if "csv" in n or "parse_csv" in n:
            return "parse_csv"
        if "xml" in n:
            return "parse_xml"
        if "json" in n:
            return "parse_json"
        return "connector_action"

    # CSV / JSON parsers (sometimes their own provider)
    if p in ("csv_parser", "csv"):
        return "parse_csv"
    if p in ("json_parser", "json"):
        return "parse_json"

    # ERP / CRM connectors — stub as HTTP with proper label
    if p in ("netsuite", "netsuite_v2", "oracle", "sap", "dynamics", "dynamics_crm"):
        return "http_request"

    # Name-based fallback
    if n == "if_condition":
        return "choice_router"
    if n == "else_condition":
        return "choice_router_else"

    return _NAME_ACTION_MAP.get(n, "connector_action")


def _extract_condition(inp):
    """Extract a human-readable condition expression from a Workato IF input dict."""
    if not isinstance(inp, dict):
        return str(inp)[:100] if inp else ""
    # Compound condition (Workato uses 'conditions' list with 'operand', 'operator', 'value')
    conds = inp.get("conditions", [])
    if conds:
        parts = []
        for c in conds:
            operand = c.get("operand", c.get("field", ""))
            operator = c.get("operator", "=")
            value = c.get("value", "")
            parts.append(f"{operand} {operator} {value}".strip())
        joiner = " AND " if inp.get("match") != "any" else " OR "
        return joiner.join(parts)
    # Simple single-condition
    operand = inp.get("operand", inp.get("field", ""))
    operator = inp.get("operator", "=")
    value = inp.get("value", "")
    if operand:
        return f"{operand} {operator} {value}".strip()
    return ""


def _classify_salesforce_op(name):
    """Map Workato Salesforce action name to a canonical Salesforce operation."""
    n = (name or "").lower()
    if "upsert" in n:
        return "UPSERT"
    if "create" in n or "insert" in n:
        return "CREATE"
    if "update" in n:
        return "UPDATE"
    if "delete" in n:
        return "DELETE"
    if "query" in n or "search" in n or "get" in n or "find" in n or "lookup" in n:
        return "QUERY"
    return "UPSERT"


# ─── Step extraction ──────────────────────────────────────────────────────────

def extract_steps(block, sequence_start=1):
    """
    Recursively extract and classify steps from a Workato recipe block.
    Pairs if_condition / else_condition into a single choice_router step
    with true_steps / false_steps.
    """
    steps = []
    seq = sequence_start
    items = list(block or [])
    i = 0

    while i < len(items):
        item = items[i]
        provider = item.get("provider", "")
        name = item.get("name", "")
        keyword = item.get("keyword", "")
        inp = item.get("input", {}) or {}
        label = item.get("title") or item.get("as") or name or provider

        canon_type = classify_step(provider, name, keyword)

        # ── IF/ELSE pairing ──────────────────────────────────────────────
        if canon_type == "choice_router":
            true_steps = extract_steps(item.get("block", []), seq + 1)
            false_steps = []
            condition_text = _extract_condition(inp)

            # Look ahead: consume an immediately following else/elsif
            if i + 1 < len(items):
                next_type = classify_step(
                    items[i + 1].get("provider", ""),
                    items[i + 1].get("name", ""),
                    items[i + 1].get("keyword", ""),
                )
                if next_type == "choice_router_else":
                    i += 1
                    false_steps = extract_steps(items[i].get("block", []), seq + 1)

            step = {
                "source_tag": f"{provider}:{name}",
                "type": "choice_router",
                "label": label or "If condition",
                "config_ref": provider,
                "requires_review": False,
                "workato_provider": provider,
                "workato_name": name,
                "complexity": "medium",
                "sequence": seq,
                "condition": condition_text,
                "true_steps": true_steps,
                "false_steps": false_steps,
            }
            steps.append(step)
            seq += 1
            i += 1
            continue

        # Skip orphaned else_condition (consumed above if paired)
        if canon_type == "choice_router_else":
            i += 1
            continue

        # ── Build common step dict ────────────────────────────────────────
        step = {
            "source_tag": f"{provider}:{name}",
            "type": canon_type,
            "label": label,
            "config_ref": provider,
            "requires_review": canon_type in ("connector_action", "custom_script"),
            "workato_provider": provider,
            "workato_name": name,
            "complexity": "low",
            "sequence": seq,
        }

        # ── Type-specific enrichment ──────────────────────────────────────

        if canon_type == "salesforce_action":
            step["salesforce_operation"] = _classify_salesforce_op(name)
            step["salesforce_object"] = (
                inp.get("sobject_name") or inp.get("object_name") or ""
            )
            step["requires_review"] = False

        elif canon_type == "http_request":
            raw_method = name.upper() if name in ("get", "post", "put", "delete", "patch") else "GET"
            step["http_method"] = raw_method
            step["url"] = inp.get("url") or inp.get("base_url") or ""
            step["label"] = label or f"{provider} {raw_method}"

        elif canon_type in ("db_select", "db_insert", "db_update", "db_delete"):
            table_raw = inp.get("schema_table_name", "")
            step["table"] = table_raw.split(".")[-1] if "." in table_raw else table_raw
            step["has_parameters"] = bool(inp.get("where_clause") or inp.get("input"))
            step["workato_input"] = inp
            step["sql_operation"] = {
                "db_select": "SELECT", "db_insert": "INSERT",
                "db_update": "UPDATE", "db_delete": "DELETE",
            }.get(canon_type, "")

        elif canon_type == "set_variable":
            step["variable_name"] = (
                inp.get("list_name") or inp.get("variable_name") or label
            )
            step["variable_value"] = inp.get("value", "")
            step["requires_review"] = False

        elif canon_type == "google_sheets_action":
            step["spreadsheet_id"] = inp.get("spreadsheet_id", "")
            step["sheet_name"]     = inp.get("sheet_name", "")
            step["http_method"]    = "POST"
            step["url"]            = "https://sheets.googleapis.com/v4/spreadsheets"
            step["requires_review"] = False

        elif canon_type == "send_email":
            step["email_to"]      = inp.get("to", inp.get("email", ""))
            step["email_subject"] = inp.get("subject", label)
            step["email_body"]    = inp.get("body", inp.get("message", ""))
            step["requires_review"] = False

        elif canon_type == "file_read":
            step["file_path"]     = inp.get("path", inp.get("file_path", ""))
            step["file_encoding"] = inp.get("encoding", "UTF-8")
            step["requires_review"] = False

        elif canon_type == "file_write":
            step["file_path"]     = inp.get("path", inp.get("file_path", ""))
            step["requires_review"] = False

        elif canon_type in ("parse_csv", "parse_json", "parse_xml"):
            step["requires_review"] = False

        elif canon_type == "log_message":
            step["message"] = inp.get("message") or inp.get("text") or label
            step["requires_review"] = False

        elif canon_type == "try_catch":
            step["monitored_steps"] = extract_steps(item.get("block", []), seq + 1)
            step["complexity"] = "high"
            step["requires_review"] = False

        elif canon_type == "loop":
            step["loop_steps"] = extract_steps(item.get("block", []), seq + 1)
            step["loop_over"] = inp.get("list", inp.get("source", ""))
            step["complexity"] = "medium"

        elif canon_type == "return_response":
            step["static_content"] = inp.get("response_body", "")
            step["status_code"] = inp.get("response_status_code", "200")
            step["requires_review"] = False

        elif canon_type == "transform":
            step["requires_review"] = True

        elif canon_type == "connector_action":
            # Unknown/unmapped connector — preserve details for reviewer
            step["requires_review"] = True
            step["label"] = f"{provider}: {label}" if provider and provider != label else label

        steps.append(step)
        seq += 1
        i += 1

    return steps


# ─── Trigger extraction ───────────────────────────────────────────────────────

def extract_trigger(code):
    """Extract canonical trigger from Workato recipe code block."""
    if not code or code.get("keyword") != "trigger":
        return {"type": "unknown", "requires_review": True}

    provider = code.get("provider", "")
    name = code.get("name", "")
    inp = code.get("input", {}) or {}

    # recipe_function triggers are callable APIs regardless of name
    if "recipe_function" in provider.lower() or "callable" in provider.lower():
        canon_type = "http_listener"
    else:
        canon_type = TRIGGER_TYPE_MAP.get(name, "unknown")
        # Any connector-based event trigger with a known provider → connector_trigger
        if canon_type == "unknown" and provider and provider not in ("", "workato", "clock"):
            canon_type = "connector_trigger"

    trigger = {
        "source_tag": f"{provider}:{name}",
        "type": canon_type,
        "label": code.get("title") or code.get("as") or name,
        "requires_review": canon_type == "unknown",
        "workato_provider": provider,
        "workato_name": name,
    }

    if canon_type == "http_listener":
        trigger["http_method"] = inp.get("http_method", "GET").upper()
        trigger["path"] = inp.get("request_url_suffix", "/endpoint")
        trigger["has_request_body"] = trigger["http_method"] in ("POST", "PUT", "PATCH")
    elif canon_type == "scheduler":
        trigger["schedule"] = inp

    return trigger


def parse_recipe_code(code_str):
    if isinstance(code_str, dict):
        return code_str
    if isinstance(code_str, str):
        try:
            return json.loads(code_str)
        except json.JSONDecodeError:
            return {}
    return {}


# ─── Recipe analyzer ──────────────────────────────────────────────────────────

def analyze_recipe(recipe_data):
    """Analyze a single Workato recipe object → normalized flow spec."""
    name = recipe_data.get("name", "unknown")
    recipe_id = recipe_data.get("id")
    description = recipe_data.get("description", "")

    code_raw = recipe_data.get("code", {})
    code = parse_recipe_code(code_raw)

    trigger = extract_trigger(code)
    steps = extract_steps(code.get("block", []))

    # Extract connections from config
    config_raw = recipe_data.get("config", [])
    config = config_raw if isinstance(config_raw, list) else []
    if isinstance(config_raw, str):
        try:
            config = json.loads(config_raw)
        except Exception:
            config = []

    connections_used = list({
        c.get("provider") for c in config
        if c.get("keyword") == "application" and c.get("provider") not in ("workato",)
    })

    return {
        "name": name,
        "source_name": name,
        "recipe_id": recipe_id,
        "description": description,
        "flow_type": "primary",
        "trigger": trigger,
        "steps": steps,
        "connections_used": connections_used,
        "error_handling": {"has_error_handler": False, "strategies": []},
    }


# ─── Workato API helpers ──────────────────────────────────────────────────────

def workato_client(api_token, email=None):
    """Return a (session, base_url) tuple with region-correct URL and auth headers."""
    api_token = "".join(api_token.split())   # strip all embedded whitespace
    try:
        import requests
        s = requests.Session()
        if api_token.startswith("wrk"):
            s.headers.update({"Authorization": f"Bearer {api_token}"})
        else:
            s.headers.update({"x-user-token": api_token})
            if email:
                s.headers.update({"x-user-email": email})
        s.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        base_url = _base_url_for_token(api_token)
        return s, base_url
    except ImportError:
        print("ERROR: pip install requests", file=sys.stderr)
        sys.exit(1)


def list_folders(session, base_url):
    r = session.get(f"{base_url}/folders")
    r.raise_for_status()
    data = r.json()
    return data.get("items", data) if isinstance(data, dict) else data


def list_recipes(session, base_url, folder_id=None):
    params = {}
    if folder_id:
        params["folder_id"] = str(folder_id)
    r = session.get(f"{base_url}/recipes", params=params)
    r.raise_for_status()
    data = r.json()
    return data.get("items", data) if isinstance(data, dict) else data


def get_recipe(session, base_url, recipe_id):
    r = session.get(f"{base_url}/recipes/{recipe_id}")
    r.raise_for_status()
    return r.json()


def resolve_folder_id(session, base_url, folder_name_or_id):
    """Resolve folder name to numeric ID."""
    try:
        return int(folder_name_or_id)
    except (ValueError, TypeError):
        pass
    folders = list_folders(session, base_url)
    for folder in folders:
        if folder.get("name", "").lower() == folder_name_or_id.lower():
            return folder["id"]
    available = [f.get("name", "") for f in folders if f.get("name")]
    raise ValueError(
        f"Folder '{folder_name_or_id}' not found in Workato account.\n"
        f"  Available folders: {', '.join(available) if available else '(none returned)'}"
    )


# ─── Analysis entry points ────────────────────────────────────────────────────

def analyze_from_api(api_token, email, folder_name_or_id, project_name, output_path):
    session, base_url = workato_client(api_token, email)
    print(f"Connecting to Workato API ({base_url})...")

    if folder_name_or_id:
        folder_id = resolve_folder_id(session, base_url, folder_name_or_id)
        print(f"  Folder resolved: {folder_name_or_id} -> id {folder_id}")
    else:
        folder_id = None
        print("  No folder specified -- pulling all recipes")

    print(f"Listing recipes{' in folder ' + str(folder_id) if folder_id else ''}...")
    recipes_meta = list_recipes(session, base_url, folder_id)
    print(f"  Found {len(recipes_meta)} recipes")

    flows = []
    for meta in recipes_meta:
        rid = meta.get("id")
        rname = meta.get("name", "unknown")
        print(f"  Fetching recipe: {rname} ({rid})...")
        try:
            full = get_recipe(session, base_url, rid)
            recipe_obj = full.get("recipe", full)
            flow = analyze_recipe(recipe_obj)
            flows.append(flow)
        except Exception as e:
            print(f"    WARNING: {e}", file=sys.stderr)

    return _build_spec(flows, project_name, output_path,
                       source_files=[f"workato:folder/{folder_id}"])


def analyze_from_files(source_dir, project_name, output_path):
    files = sorted([
        os.path.join(source_dir, f)
        for f in os.listdir(source_dir)
        if f.endswith(".json")
    ])
    print(f"Found {len(files)} JSON files in {source_dir}")

    flows = []
    for fpath in files:
        print(f"  Analyzing {os.path.basename(fpath)}...")
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
            recipe_obj = data.get("recipe", data)
            flow = analyze_recipe(recipe_obj)
            flows.append(flow)
        except Exception as e:
            print(f"    WARNING: {e}", file=sys.stderr)

    return _build_spec(flows, project_name, output_path, source_files=files)


def _build_spec(flows, project_name, output_path, source_files):
    gaps = []
    for flow in flows:
        if flow["trigger"].get("requires_review"):
            gaps.append({"flow": flow["name"], "component": "trigger",
                         "note": "Unknown trigger type"})
        for step in _iter_all_steps(flow.get("steps", [])):
            if step.get("requires_review"):
                gaps.append({
                    "flow": flow["name"],
                    "step_type": step.get("type"),
                    "sequence": step.get("sequence"),
                    "label": step.get("label", ""),
                    "note": "Manual review required",
                })

    spec = {
        "schema_version": "1.0",
        "source_system": "workato",
        "source_version": "workato_cloud",
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "project_name": project_name,
        "source_files_analyzed": [str(s) for s in source_files],
        "summary": {
            "total_flows": len(flows),
            "primary_flows": len(flows),
            "sub_flows": 0,
            "total_connections": 0,
            "gaps_found": len(gaps),
            "overall_complexity": "medium",
        },
        "connections": {},
        "integrations": flows,
        "gaps": gaps,
        "migration_notes": "",
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)

    print(f"\nSpec written to: {output_path}")
    print(f"  Flows: {len(flows)}, Gaps: {len(gaps)}")
    return spec


def _iter_all_steps(steps):
    """Flatten nested steps (true_steps / false_steps / monitored_steps) for gap analysis."""
    for step in steps:
        yield step
        for key in ("true_steps", "false_steps", "monitored_steps", "loop_steps"):
            yield from _iter_all_steps(step.get(key, []))


# ─── .env helper ─────────────────────────────────────────────────────────────

def _load_env_token(key):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if not os.path.isfile(env_path):
        return None
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Analyze Workato recipes into a migration spec.")
    parser.add_argument("--folder", help="Workato folder name or ID (requires API creds)")
    parser.add_argument("--folder-id", help="Workato folder ID (numeric)")
    parser.add_argument("--source-dir", help="Local directory of exported Workato recipe JSON files")
    parser.add_argument("--project", required=True, help="Project name")
    parser.add_argument("--output", help="Output spec path")
    args = parser.parse_args()

    project_name = args.project
    output_path = args.output or os.path.join("migration-specs", f"{project_name}.json")

    if args.source_dir:
        analyze_from_files(args.source_dir, project_name, output_path)
    else:
        api_token = os.environ.get("WORKATO_API_TOKEN") or _load_env_token("WORKATO_API_TOKEN")
        email = os.environ.get("WORKATO_EMAIL") or _load_env_token("WORKATO_EMAIL")
        if not api_token:
            if not (args.folder or args.folder_id):
                parser.error("Provide --folder, --folder-id, or --source-dir "
                             "(or set WORKATO_API_TOKEN for live pull)")
            print("ERROR: WORKATO_API_TOKEN not set", file=sys.stderr)
            sys.exit(1)
        folder = args.folder or args.folder_id or None
        analyze_from_api(api_token, email, folder, project_name, output_path)


if __name__ == "__main__":
    main()
