#!/usr/bin/env python3
"""
Celigo Analyzer
Pulls integrations/flows from a Celigo account via the integrator.io API,
or reads exported Celigo flow JSON files, and produces a platform-agnostic spec.

Usage:
    # Pull live from Celigo
    python analyzers/analyze_celigo.py --integration "Customer API" --project my-project

    # Analyze exported JSON
    python analyzers/analyze_celigo.py --source-dir path/to/exports/ --project my-project

Required environment variables (live pull):
    CELIGO_API_TOKEN   Bearer token from integrator.io → Settings → API tokens
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone


CELIGO_BASE_URL = "https://api.integrator.io/v1"

# Celigo application/adaptor names → canonical connection type
CELIGO_APP_MAP = {
    "rest":             "http",
    "http":             "http",
    "postgresql":       "db",
    "mysql":            "db",
    "mssql":            "db",
    "mongodb":          "db",
    "salesforce":       "salesforce",
    "netsuite":         "erp",
    "shopify":          "ecommerce",
    "ftp":              "ftp",
    "sftp":             "sftp",
    "s3":               "object_storage",
    "webhook":          "http_listener",
    "restlistener":     "http_listener",
}

# Celigo step type → canonical step type
CELIGO_STEP_MAP = {
    "export":       "http_request",      # Celigo "exports" read from a source
    "import":       "db_insert",         # Celigo "imports" write to a target
    "transform":    "transform",
    "filter":       "choice_router",
    "lookup":       "transform",
    "hook":         "custom_script",
    "branching":    "choice_router",
    "merge":        "branch",
    "mapping":      "transform",
}


def celigo_session(api_token):
    try:
        import requests
        s = requests.Session()
        s.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        return s
    except ImportError:
        print("ERROR: pip install requests", file=sys.stderr)
        sys.exit(1)


def analyze_celigo_flow(flow_data):
    """Convert a Celigo flow definition to a canonical flow spec."""
    name = flow_data.get("name", "unknown")
    flow_id = flow_data.get("_id", "")
    description = flow_data.get("description", "")

    # Determine trigger
    page_generators = flow_data.get("pageGenerators", [])
    trigger = {"type": "unknown", "requires_review": True}
    if page_generators:
        pg = page_generators[0]
        app_type = pg.get("adaptorType", pg.get("type", "")).lower()
        canon_type = CELIGO_APP_MAP.get(app_type, "connector_trigger")
        if "listener" in app_type or "webhook" in app_type:
            canon_type = "http_listener"
        trigger = {
            "source_tag": f"celigo:{app_type}",
            "type": canon_type,
            "label": pg.get("name", app_type),
            "config_ref": pg.get("_exportId"),
            "requires_review": canon_type not in ("http_listener", "scheduler"),
            "celigo_export_id": pg.get("_exportId"),
        }

    # Steps from page processors
    steps = []
    seq = 1
    for pp in flow_data.get("pageProcessors", []):
        pp_type = pp.get("type", "export")
        app_type = pp.get("adaptorType", "").lower()
        canon_type = CELIGO_STEP_MAP.get(pp_type, "connector_action")

        # If it's an import with a DB adaptor, map to db_insert/update
        if pp_type == "import" and app_type in ("postgresql", "mysql", "mssql", "mongodb"):
            canon_type = "db_insert"

        step = {
            "source_tag": f"celigo:{pp_type}:{app_type}",
            "type": canon_type,
            "label": pp.get("name", pp_type),
            "config_ref": pp.get("_exportId") or pp.get("_importId"),
            "requires_review": canon_type in ("connector_action", "custom_script"),
            "celigo_type": pp_type,
            "celigo_adaptor": app_type,
            "complexity": "medium" if pp_type in ("hook", "transform") else "low",
            "sequence": seq,
        }
        steps.append(step)
        seq += 1

    return {
        "name": name,
        "source_name": name,
        "flow_id": flow_id,
        "description": description,
        "flow_type": "primary",
        "trigger": trigger,
        "steps": steps,
        "connections_used": [],
        "error_handling": {"has_error_handler": False, "strategies": []},
    }


def analyze_from_api(api_token, integration_name, project_name, output_path):
    session = celigo_session(api_token)
    print("Connecting to Celigo API...")

    # Get all integrations
    r = session.get(f"{CELIGO_BASE_URL}/integrations")
    r.raise_for_status()
    integrations = r.json()

    target = next((i for i in integrations if i.get("name", "") == integration_name), None)
    if not target:
        print(f"ERROR: Integration '{integration_name}' not found. Available:")
        for i in integrations[:10]:
            print(f"  - {i.get('name')}")
        sys.exit(1)

    int_id = target["_id"]
    print(f"  Found integration: {integration_name} (id: {int_id})")

    # Get all flows for this integration
    r = session.get(f"{CELIGO_BASE_URL}/flows", params={"_integrationId": int_id})
    r.raise_for_status()
    flows_raw = r.json()
    print(f"  Found {len(flows_raw)} flows")

    flows = [analyze_celigo_flow(f) for f in flows_raw]
    return _build_spec(flows, project_name, output_path,
                       source_files=[f"celigo:integration/{int_id}"])


def analyze_from_files(source_dir, project_name, output_path):
    files = sorted([
        os.path.join(source_dir, f)
        for f in os.listdir(source_dir)
        if f.endswith(".json")
    ])
    print(f"Found {len(files)} JSON files in {source_dir}")
    flows = []
    for fpath in files:
        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)
        flow_list = data if isinstance(data, list) else [data]
        for flow_data in flow_list:
            flows.append(analyze_celigo_flow(flow_data))
    return _build_spec(flows, project_name, output_path, source_files=files)


def _build_spec(flows, project_name, output_path, source_files):
    gaps = []
    for flow in flows:
        if flow["trigger"].get("requires_review"):
            gaps.append({"flow": flow["name"], "component": "trigger"})
        for step in flow.get("steps", []):
            if step.get("requires_review"):
                gaps.append({"flow": flow["name"], "step_type": step.get("type"), "sequence": step.get("sequence")})

    spec = {
        "schema_version": "1.0",
        "source_system": "celigo",
        "source_version": "integrator.io",
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
        "migration_notes": "Celigo exports and imports map to source/target connectors. Review each step's adaptor type.",
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)
    print(f"\nSpec written to: {output_path}")
    return spec


def main():
    parser = argparse.ArgumentParser(description="Analyze Celigo integrations into a migration spec.")
    parser.add_argument("--integration", help="Celigo integration name (for live pull)")
    parser.add_argument("--source-dir", help="Directory of exported Celigo JSON files")
    parser.add_argument("--project", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    output_path = args.output or os.path.join("migration-specs", f"{args.project}.json")

    if args.source_dir:
        analyze_from_files(args.source_dir, args.project, output_path)
    elif args.integration:
        token = os.environ.get("CELIGO_API_TOKEN")
        if not token:
            print("ERROR: CELIGO_API_TOKEN not set", file=sys.stderr)
            sys.exit(1)
        analyze_from_api(token, args.integration, args.project, output_path)
    else:
        parser.error("Provide --integration or --source-dir")


if __name__ == "__main__":
    main()
