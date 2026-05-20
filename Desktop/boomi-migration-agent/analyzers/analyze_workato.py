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

# Workato trigger name → canonical trigger type
TRIGGER_TYPE_MAP = {
    "callable_recipe":          "http_listener",
    "webhook_event":            "http_listener",
    "receive_http_request":     "http_listener",
    "scheduled_event":          "scheduler",
    "new_record":               "db_trigger",
    "updated_record":           "db_trigger",
    "new_message":              "event_trigger",
    "new_event":                "event_trigger",
}

# Workato action name → canonical step type
ACTION_TYPE_MAP = {
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
    "if_condition":             "choice_router",
    "else_condition":           "choice_router",
    "repeat_while":             "loop",
    "map_list":                 "transform",
    "custom_action":            "custom_script",
}


def workato_client(api_token, email=None):
    """Return a requests Session with the correct auth headers.
    wrkaus-/wrkeu-/wrkjp- prefixed tokens use Bearer auth.
    Legacy tokens use x-user-token + x-user-email.
    """
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
        return s
    except ImportError:
        print("ERROR: pip install requests", file=sys.stderr)
        sys.exit(1)


def list_folders(session):
    r = session.get(f"{WORKATO_BASE_URL}/folders")
    r.raise_for_status()
    data = r.json()
    return data.get("items", data) if isinstance(data, dict) else data


def list_recipes(session, folder_id=None):
    params = {}
    if folder_id:
        params["folder_id"] = str(folder_id)
    r = session.get(f"{WORKATO_BASE_URL}/recipes", params=params)
    r.raise_for_status()
    data = r.json()
    return data.get("items", data) if isinstance(data, dict) else data


def get_recipe(session, recipe_id):
    r = session.get(f"{WORKATO_BASE_URL}/recipes/{recipe_id}")
    r.raise_for_status()
    return r.json()


def resolve_folder_id(session, folder_name_or_id):
    """Resolve folder name to ID."""
    try:
        return int(folder_name_or_id)
    except (ValueError, TypeError):
        pass
    for folder in list_folders(session):
        if folder.get("name", "").lower() == folder_name_or_id.lower():
            return folder["id"]
    raise ValueError(f"Folder '{folder_name_or_id}' not found in Workato account")


def parse_recipe_code(code_str):
    """Parse the recipe code JSON string into a Python object."""
    if isinstance(code_str, dict):
        return code_str
    if isinstance(code_str, str):
        try:
            return json.loads(code_str)
        except json.JSONDecodeError:
            return {}
    return {}


def extract_trigger(code):
    """Extract canonical trigger from Workato recipe code."""
    if not code or code.get("keyword") != "trigger":
        return {"type": "unknown", "requires_review": True}

    provider = code.get("provider", "")
    name = code.get("name", "")
    inp = code.get("input", {})

    canon_type = TRIGGER_TYPE_MAP.get(name, "unknown")

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


def extract_steps(block, sequence_start=1):
    """Recursively extract steps from a recipe block."""
    steps = []
    seq = sequence_start

    for item in (block or []):
        provider = item.get("provider", "")
        name = item.get("name", "")
        inp = item.get("input", {})
        label = item.get("title") or item.get("as") or name
        canon_type = ACTION_TYPE_MAP.get(name, "connector_action")

        step = {
            "source_tag": f"{provider}:{name}",
            "type": canon_type,
            "label": label,
            "config_ref": provider,
            "requires_review": canon_type in ("unknown", "connector_action", "custom_script"),
            "workato_provider": provider,
            "workato_name": name,
            "complexity": "low",
            "sequence": seq,
        }

        # DB steps
        if canon_type in ("db_select", "db_insert", "db_update", "db_delete"):
            table_raw = inp.get("schema_table_name", "")
            table = table_raw.split(".")[-1] if "." in table_raw else table_raw
            step["table"] = table
            step["has_parameters"] = bool(inp.get("where_clause") or inp.get("input"))
            step["workato_input"] = inp
            step["sql_operation"] = {"db_select": "SELECT", "db_insert": "INSERT",
                                     "db_update": "UPDATE", "db_delete": "DELETE"}.get(canon_type, "")

        # HTTP steps
        elif canon_type == "http_request":
            step["http_method"] = name.upper() if name in ("get", "post", "put", "delete", "patch") else "GET"
            step["url"] = inp.get("url", inp.get("base_url", ""))

        # Conditional blocks
        elif canon_type == "choice_router":
            nested = extract_steps(item.get("block", []), seq + 1)
            step["nested_steps"] = nested
            step["condition"] = inp

        # Return response
        elif canon_type == "return_response":
            step["static_content"] = inp.get("response_body", "")
            step["status_code"] = inp.get("response_status_code", "200")

        steps.append(step)
        seq += 1

    return steps


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


def analyze_from_api(api_token, email, folder_name_or_id, project_name, output_path):
    """Pull recipes from Workato API and analyze them."""
    session = workato_client(api_token, email)

    print(f"Connecting to Workato API...")
    folder_id = resolve_folder_id(session, folder_name_or_id)
    print(f"  Folder resolved: {folder_name_or_id} -> id {folder_id}")

    print(f"Listing recipes in folder {folder_id}...")
    recipes_meta = list_recipes(session, folder_id)
    print(f"  Found {len(recipes_meta)} recipes")

    flows = []
    for meta in recipes_meta:
        rid = meta.get("id")
        rname = meta.get("name", "unknown")
        print(f"  Fetching recipe: {rname} ({rid})...")
        try:
            full = get_recipe(session, rid)
            recipe_obj = full.get("recipe", full)
            flow = analyze_recipe(recipe_obj)
            flows.append(flow)
        except Exception as e:
            print(f"    WARNING: {e}", file=sys.stderr)

    return _build_spec(flows, project_name, output_path, source_files=[f"workato:folder/{folder_id}"])


def analyze_from_files(source_dir, project_name, output_path):
    """Analyze exported Workato recipe JSON files from a local directory."""
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
            gaps.append({"flow": flow["name"], "component": "trigger", "note": "Unknown trigger type"})
        for step in flow.get("steps", []):
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


def main():
    parser = argparse.ArgumentParser(description="Analyze Workato recipes into a migration spec.")
    parser.add_argument("--folder", help="Workato folder name or ID to pull from (requires API creds)")
    parser.add_argument("--folder-id", help="Workato folder ID (numeric)")
    parser.add_argument("--source-dir", help="Local directory of exported Workato recipe JSON files")
    parser.add_argument("--project", required=True, help="Project name")
    parser.add_argument("--output", help="Output spec path")
    args = parser.parse_args()

    project_name = args.project
    output_path = args.output or os.path.join("migration-specs", f"{project_name}.json")

    if args.source_dir:
        analyze_from_files(args.source_dir, project_name, output_path)
    elif args.folder or args.folder_id:
        api_token = os.environ.get("WORKATO_API_TOKEN")
        email = os.environ.get("WORKATO_EMAIL")
        if not api_token:
            print("ERROR: WORKATO_API_TOKEN not set", file=sys.stderr)
            sys.exit(1)
        folder = args.folder or args.folder_id
        analyze_from_api(api_token, email, folder, project_name, output_path)
    else:
        parser.error("Provide --folder, --folder-id, or --source-dir")


if __name__ == "__main__":
    main()
