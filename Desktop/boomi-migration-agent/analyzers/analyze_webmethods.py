#!/usr/bin/env python3
"""
webMethods Analyzer
Reads webMethods Integration Server flow services (XML) or webMethods.io
workflow JSON exports and produces a platform-agnostic migration spec.

Usage:
    # Analyze webMethods IS flow service XML files
    python analyzers/analyze_webmethods.py --source-dir path/to/packages/ --project my-project

    # Pull from webMethods.io (cloud)
    python analyzers/analyze_webmethods.py --wmio-project "My Project" --project my-project

Required environment variables (webMethods.io pull):
    WMIO_TENANT_URL     e.g. https://mycompany.int-aws-us.webmethods.io
    WMIO_USERNAME       webMethods.io username
    WMIO_PASSWORD       webMethods.io password
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


# webMethods IS built-in services → canonical step type
WM_SERVICE_MAP = {
    "pub.flow:sequence":            "sequence",
    "pub.flow:branch":              "choice_router",
    "pub.flow:loop":                "loop",
    "pub.flow:invoke":              "subprocess_call",
    "pub.flow:exit":                "exception",
    "pub.flow:map":                 "transform",
    "pub.client:http":              "http_request",
    "pub.client:httpsClient":       "http_request",
    "pub.soap.handler:invoke":      "http_request",
    "pub.db.jdbc:call":             "db_select",
    "pub.db.jdbc:select":           "db_select",
    "pub.db.jdbc:insert":           "db_insert",
    "pub.db.jdbc:update":           "db_update",
    "pub.db.jdbc:delete":           "db_delete",
    "pub.jms:send":                 "event_trigger",
    "pub.jms:receive":              "event_trigger",
    "pub.transform:transformValues":"transform",
    "pub.string:concat":            "transform",
    "WmPublic:pub.flow:sequence":   "sequence",
}

# webMethods.io step types → canonical
WMIO_STEP_MAP = {
    "trigger":      "http_listener",
    "action":       "connector_action",
    "condition":    "choice_router",
    "transformer":  "transform",
    "loop":         "loop",
    "parallel":     "branch",
    "sub_workflow": "subprocess_call",
}


def analyze_wm_flow_xml(fpath):
    """Analyze a webMethods IS flow service XML file."""
    try:
        tree = ET.parse(fpath)
        root = tree.getroot()
    except ET.ParseError as e:
        return None

    name = Path(fpath).stem
    steps = []
    seq = 1

    for elem in root.iter():
        tag = elem.tag
        svc = elem.get("SERVICE") or elem.get("service") or ""
        sig_name = elem.get("SIGTYPE") or elem.get("name") or ""

        # Map to canonical
        canon_type = WM_SERVICE_MAP.get(svc, None)
        if canon_type is None and tag in ("SEQUENCE", "BRANCH", "LOOP", "INVOKE", "EXIT", "MAP"):
            canon_type = {
                "SEQUENCE": "sequence",
                "BRANCH": "choice_router",
                "LOOP": "loop",
                "INVOKE": "subprocess_call",
                "EXIT": "exception",
                "MAP": "transform",
            }.get(tag, "connector_action")

        if canon_type is None:
            continue

        step = {
            "source_tag": f"wm:{tag}:{svc}",
            "type": canon_type,
            "label": elem.get("COMMENT") or svc or tag,
            "config_ref": svc,
            "requires_review": canon_type in ("connector_action", "subprocess_call"),
            "wm_service": svc,
            "wm_tag": tag,
            "complexity": "medium" if canon_type in ("transform", "choice_router") else "low",
            "sequence": seq,
        }

        # DB steps — extract SQL if present
        if canon_type in ("db_select", "db_insert", "db_update", "db_delete"):
            sql_elem = elem.find(".//VALUE[@name='sql']") or elem.find(".//sql")
            if sql_elem is not None:
                step["sql"] = (sql_elem.text or "").strip()

        steps.append(step)
        seq += 1

    # Trigger — webMethods IS services are triggered by HTTP or JMS or scheduler
    trigger = {
        "type": "http_listener",
        "source_tag": "wm:http:trigger",
        "label": f"IS Service: {name}",
        "requires_review": True,
        "note": "webMethods IS trigger type needs manual verification (HTTP, JMS, or scheduler)",
    }

    return {
        "name": name,
        "source_name": name,
        "flow_type": "primary",
        "trigger": trigger,
        "steps": steps,
        "connections_used": [],
        "error_handling": {"has_error_handler": False, "strategies": []},
    }


def analyze_wmio_workflow(wf_data):
    """Analyze a webMethods.io workflow JSON export."""
    name = wf_data.get("name", "unknown")
    wf_id = wf_data.get("uid", "")

    trigger = {"type": "unknown", "requires_review": True}
    steps = []
    seq = 0

    for node in wf_data.get("nodes", []):
        node_type = node.get("type", "action")
        canon_type = WMIO_STEP_MAP.get(node_type, "connector_action")
        label = node.get("name", node_type)

        if node_type == "trigger":
            trigger = {
                "source_tag": f"wmio:trigger",
                "type": "http_listener",
                "label": label,
                "requires_review": False,
            }
        else:
            seq += 1
            steps.append({
                "source_tag": f"wmio:{node_type}",
                "type": canon_type,
                "label": label,
                "config_ref": node.get("service"),
                "requires_review": canon_type in ("connector_action", "custom_script"),
                "wmio_service": node.get("service"),
                "complexity": "medium",
                "sequence": seq,
            })

    return {
        "name": name,
        "source_name": name,
        "workflow_id": wf_id,
        "flow_type": "primary",
        "trigger": trigger,
        "steps": steps,
        "connections_used": [],
        "error_handling": {"has_error_handler": False, "strategies": []},
    }


def analyze_from_files(source_dir, project_name, output_path):
    source_dir = os.path.abspath(source_dir)
    flows = []

    for dirpath, _, filenames in os.walk(source_dir):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            if fname.endswith(".xml"):
                print(f"  Analyzing IS flow: {fname}")
                flow = analyze_wm_flow_xml(fpath)
                if flow and flow.get("steps"):
                    flows.append(flow)
            elif fname.endswith(".json"):
                print(f"  Analyzing webMethods.io workflow: {fname}")
                try:
                    with open(fpath, encoding="utf-8") as f:
                        data = json.load(f)
                    wf_list = data if isinstance(data, list) else [data]
                    for wf in wf_list:
                        flows.append(analyze_wmio_workflow(wf))
                except Exception as e:
                    print(f"    WARNING: {e}", file=sys.stderr)

    return _build_spec(flows, project_name, output_path, [source_dir])


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
        "source_system": "webmethods",
        "source_version": "webmethods_is_or_io",
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "project_name": project_name,
        "source_files_analyzed": [str(s) for s in source_files],
        "summary": {
            "total_flows": len(flows),
            "primary_flows": len(flows),
            "sub_flows": 0,
            "total_connections": 0,
            "gaps_found": len(gaps),
            "overall_complexity": "high" if gaps else "medium",
        },
        "connections": {},
        "integrations": flows,
        "gaps": gaps,
        "migration_notes": "webMethods IS INVOKE steps referencing custom packages require manual review.",
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)
    print(f"\nSpec written to: {output_path}")
    return spec


def main():
    parser = argparse.ArgumentParser(description="Analyze webMethods IS or webMethods.io projects.")
    parser.add_argument("--source-dir", help="Directory of webMethods IS packages or wmio exports")
    parser.add_argument("--wmio-project", help="webMethods.io project name (requires API creds)")
    parser.add_argument("--project", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    output_path = args.output or os.path.join("migration-specs", f"{args.project}.json")

    if args.source_dir:
        analyze_from_files(args.source_dir, args.project, output_path)
    else:
        parser.error("Provide --source-dir (wmio live pull coming soon)")


if __name__ == "__main__":
    main()
