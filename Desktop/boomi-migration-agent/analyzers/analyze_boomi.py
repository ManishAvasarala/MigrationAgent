#!/usr/bin/env python3
"""
Boomi Process Analyzer
Reads a Boomi active-development directory and produces a platform-agnostic
migration spec (same format as analyze_mulesoft.py output).

Usage:
    python analyzers/analyze_boomi.py active-development/
    python analyzers/analyze_boomi.py active-development/ --output migration-specs/boomi-customer-api.json
    python analyzers/analyze_boomi.py active-development/ --project my-project
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


BOOMI_NS = "http://api.platform.boomi.com/"

WSS_OP_TYPE_TO_HTTP = {
    "QUERY": "GET",
    "CREATE": "POST",
    "UPDATE": "PUT",
    "DELETE": "DELETE",
}

DB_OP_TYPE_TO_STEP_TYPE = {
    "GET": "db_select",
    "CREATE": "db_insert",
    "UPDATE": "db_update",
    "DELETE": "db_delete",
    "EXECUTE": "db_select",  # Boomi uses EXECUTE for Standard Get
}

SHAPE_TYPE_LABELS = {
    "start": "trigger",
    "map": "transform",
    "dataprocess": "custom_script",
    "message": "set_payload",
    "documentproperties": "set_variable",
    "decision": "choice_router",
    "route": "choice_router",
    "branch": "branch",
    "stop": "exception",
    "notify": "logger",
    "processcall": "subprocess_call",
    "returndocuments": "return_response",
    "connectoraction": "connector_action",
}


def build_component_index(directory):
    """
    Walk the directory tree and build a dict of componentId -> (file_path, ElementTree).
    This allows resolving operation/connection references by GUID.
    """
    index = {}
    for dirpath, _, filenames in os.walk(directory):
        for fname in filenames:
            if not fname.endswith(".xml"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                tree = ET.parse(fpath)
                root = tree.getroot()
                comp_id = root.get("componentId")
                if comp_id:
                    index[comp_id] = (fpath, tree)
            except ET.ParseError:
                pass
    return index


def find_elem(root, *tag_names):
    """Search for an element matching any of the tag names (no namespace)."""
    for tag in tag_names:
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == tag:
                return elem
    return None


def extract_wss_trigger(operation_id, component_index):
    """Look up WSS operation XML and extract HTTP trigger metadata."""
    if operation_id not in component_index:
        return {
            "type": "unknown",
            "source_tag": "wss:listen",
            "label": "WSS Listen",
            "requires_review": True,
            "operation_id": operation_id,
            "note": "WSS operation XML not found in index",
        }

    _, tree = component_index[operation_id]
    root = tree.getroot()

    listen_action = find_elem(root, "WebServicesServerListenAction")
    if listen_action is None:
        return {
            "type": "http_listener",
            "source_tag": "wss:listen",
            "requires_review": True,
            "operation_id": operation_id,
            "note": "Could not parse WSS operation XML",
        }

    op_type = listen_action.get("operationType", "QUERY")
    object_name = listen_action.get("objectName", "")
    input_type = listen_action.get("inputType", "none")

    http_method = WSS_OP_TYPE_TO_HTTP.get(op_type, "GET")
    has_body = input_type not in ("none",)

    # Reconstruct Boomi WSS path convention
    boomi_path = f"/ws/simple/{op_type.lower()}{object_name[0].upper() + object_name[1:] if object_name else ''}"

    # Infer clean REST path from the object name
    rest_path, path_params = infer_rest_path(object_name, http_method)

    return {
        "source_tag": "wss:listen",
        "type": "http_listener",
        "label": f"WSS Listen {http_method} {boomi_path}",
        "config_ref": None,
        "requires_review": False,
        "boomi_step": "Start_WSS_Listen",
        "boomi_component": "wss_operation",
        "complexity": "low",
        "http_method": http_method,
        "boomi_path": boomi_path,
        "path": rest_path,
        "path_params": path_params,
        "allowed_methods": http_method,
        "has_request_body": has_body,
        "boomi_operation_type": op_type,
        "boomi_object_name": object_name,
        "boomi_input_type": input_type,
    }


def infer_rest_path(object_name, http_method):
    """
    Map a Boomi WSS objectName to a clean REST path.
    Returns (path, list_of_path_params).

    Examples:
      customers  → /customers          []
      customerById → /customers/{id}   [id]
      customer   → /customers          []  (POST context)
    """
    name_lower = object_name.lower()

    # Patterns that imply a single-resource path with an ID
    if "byid" in name_lower or name_lower.endswith("id"):
        base = re.sub(r"byid$|id$", "", name_lower).rstrip("_")
        # pluralize naively
        resource = base if base.endswith("s") else base + "s"
        return f"/{resource}/{{id}}", ["id"]

    # If the name is already plural, use it as-is
    if name_lower.endswith("s"):
        return f"/{name_lower}", []

    # Singular → pluralize
    return f"/{name_lower}s", []


def parse_sql_metadata(sql):
    """
    Extract table name and parameter names from a Boomi SQL string.
    Boomi uses $param_name notation for named parameters.

    Returns:
        table      - first table name found (FROM/INTO/UPDATE)
        params     - list of parameter names (without $)
        operation  - SELECT / INSERT / UPDATE / DELETE
    """
    if not sql:
        return None, [], "UNKNOWN"

    sql_upper = sql.upper().strip()
    operation = sql_upper.split()[0] if sql_upper else "UNKNOWN"

    # Extract table name
    table = None
    for pattern in [r"FROM\s+(\w+)", r"INTO\s+(\w+)", r"UPDATE\s+(\w+)"]:
        m = re.search(pattern, sql, re.IGNORECASE)
        if m:
            table = m.group(1).lower()
            break

    # Extract $param_name placeholders
    params = re.findall(r"\$(\w+)", sql)

    return table, params, operation


def extract_db_step(shape, operation_id, connection_id, component_index, sequence):
    """Look up DB operation XML and extract full step metadata."""
    step = {
        "source_tag": "db:operation",
        "type": "db_select",
        "label": shape.get("userlabel", "DB Operation"),
        "config_ref": connection_id,
        "requires_review": False,
        "boomi_step": "DatabaseV2",
        "boomi_component": "databasev2_connection + databasev2_operation",
        "complexity": "low",
        "sql": None,
        "table": None,
        "params": [],
        "has_parameters": False,
        "operation_id": operation_id,
        "connection_id": connection_id,
        "sequence": sequence,
    }

    if operation_id not in component_index:
        step["requires_review"] = True
        step["note"] = "DB operation XML not found in component index"
        return step

    _, tree = component_index[operation_id]
    root = tree.getroot()

    # customOperationType on GenericOperationConfig tells us GET/CREATE/UPDATE/DELETE
    gen_config = find_elem(root, "GenericOperationConfig")
    if gen_config is not None:
        custom_op = gen_config.get("customOperationType", gen_config.get("operationType", "GET"))
        step["type"] = DB_OP_TYPE_TO_STEP_TYPE.get(custom_op, "db_select")
        step["boomi_step"] = f"DatabaseV2_{custom_op}"

        # SQL lives in <field id="query" type="string" value="..."/>
        for field in gen_config.findall("field"):
            if field.get("id") == "query":
                sql = field.get("value", "")
                table, params, operation = parse_sql_metadata(sql)
                step["sql"] = sql
                step["table"] = table
                step["params"] = params
                step["has_parameters"] = bool(params)
                step["sql_operation"] = operation
                break

    return step


def extract_map_step(shape, sequence):
    config = shape.find("configuration/map")
    map_id = config.get("mapId") if config is not None else None
    return {
        "source_tag": "ee:transform",
        "type": "transform",
        "label": shape.get("userlabel", "Map"),
        "config_ref": None,
        "requires_review": False,
        "boomi_step": "Map",
        "boomi_component": "transform.map + profiles",
        "complexity": "medium",
        "has_dataweave": False,
        "map_id": map_id,
        "sequence": sequence,
    }


def extract_message_step(shape, sequence):
    msg_elem = shape.find("configuration/message/msgTxt")
    msg_text = msg_elem.text if msg_elem is not None else ""
    # Strip enclosing single quotes that Boomi uses for literal JSON
    clean = msg_text.strip("'") if msg_text else ""
    return {
        "source_tag": "message:step",
        "type": "set_payload",
        "label": shape.get("userlabel", "Message"),
        "config_ref": None,
        "requires_review": False,
        "boomi_step": "Message",
        "boomi_component": "message_step",
        "complexity": "low",
        "static_content": clean,
        "sequence": sequence,
    }


def extract_set_properties_step(shape, sequence):
    props = []
    for dp in shape.findall(".//documentproperty"):
        name = dp.get("name", "")
        prop_id = dp.get("propertyId", "")
        # Find source value
        source = shape.find(f".//documentproperty[@name='{name}']/sourcevalues/parametervalue")
        source_type = source.get("valueType", "") if source is not None else ""
        source_value = ""
        if source is not None:
            pp = source.find("processparameter")
            if pp is not None:
                source_value = pp.get("processproperty", "")
        props.append({"name": name, "property_id": prop_id, "source_type": source_type, "source_value": source_value})
    return {
        "source_tag": "core:set-variable",
        "type": "set_variable",
        "label": shape.get("userlabel", "Set Properties"),
        "config_ref": None,
        "requires_review": False,
        "boomi_step": "Set_Properties",
        "boomi_component": "set_properties_step",
        "complexity": "low",
        "properties": props,
        "sequence": sequence,
    }


def extract_groovy_step(shape, sequence):
    script_elem = shape.find(".//script")
    script_text = script_elem.text if script_elem is not None else ""

    # Infer the script's purpose from its content
    purpose = "custom_transform"
    if "not found" in script_text.lower() or "404" in script_text:
        purpose = "not_found_handler"
    elif re.search(r"sb\.append|JSON\s*array|getDataCount", script_text):
        purpose = "array_combiner"
    elif "JsonSlurper" in script_text or "parseText" in script_text:
        purpose = "json_transformer"

    return {
        "source_tag": "dataprocess:groovy",
        "type": "custom_script",
        "label": shape.get("userlabel", "Groovy Script"),
        "config_ref": None,
        "requires_review": True,
        "boomi_step": "Data_Process_Groovy",
        "boomi_component": "data_process_step",
        "complexity": "high",
        "language": "groovy",
        "purpose": purpose,
        "script": script_text,
        "note": "Groovy script requires manual mapping to target platform equivalent",
        "sequence": sequence,
    }


def extract_connection_info(connection_id, component_index):
    """Extract connection metadata from a connection component XML."""
    if connection_id not in component_index:
        return {"id": connection_id, "type": "unknown", "requires_review": True}

    _, tree = component_index[connection_id]
    root = tree.getroot()

    name = root.get("name", "")
    sub_type = root.get("subType", "")

    # DatabaseV2: look for JDBC URL
    url_elem = find_elem(root, "url")
    jdbc_url = url_elem.text.strip() if url_elem is not None and url_elem.text else ""

    driver = ""
    conn_type = "unknown"
    if "postgresql" in jdbc_url.lower():
        driver = "postgresql"
        conn_type = "db"
    elif "mysql" in jdbc_url.lower():
        driver = "mysql"
        conn_type = "db"
    elif "sqlserver" in jdbc_url.lower() or "mssql" in jdbc_url.lower():
        driver = "mssql"
        conn_type = "db"
    elif "oracle" in jdbc_url.lower():
        driver = "oracle"
        conn_type = "db"
    elif sub_type == "wss":
        conn_type = "http_listener"
    elif "rest" in sub_type.lower():
        conn_type = "http"

    # Parse JDBC URL components
    host, port, database = "", "", ""
    m = re.match(r"jdbc:\w+://([^:/]+):?(\d*)/(\w+)", jdbc_url)
    if m:
        host, port, database = m.group(1), m.group(2) or "5432", m.group(3)

    return {
        "id": connection_id,
        "name": name,
        "type": conn_type,
        "driver": driver,
        "jdbc_url": jdbc_url,
        "host": host,
        "port": port,
        "database": database,
        "boomi_connector_type": sub_type,
        "requires_review": False,
    }


def analyze_process(process_file, component_index):
    """Analyze a single Boomi process XML → return a normalized flow spec dict."""
    tree = ET.parse(process_file)
    root = tree.getroot()

    name = root.get("name", Path(process_file).stem)
    component_id = root.get("componentId", "")
    description = ""
    desc_elem = find_elem(root, "description")
    if desc_elem is not None and desc_elem.text:
        description = desc_elem.text.strip()

    # Sort shapes by x-position to get execution order
    all_shapes = root.findall(".//shape")
    ordered_shapes = sorted(all_shapes, key=lambda s: float(s.get("x", "0")))

    trigger = None
    steps = []
    connections_used = set()
    seq = 0

    for shape in ordered_shapes:
        shape_type = shape.get("shapetype", "")
        userlabel = shape.get("userlabel", "")

        connector_cfg = shape.find("configuration/connectoraction")

        # ── Trigger (Start shape) ──────────────────────────────────────────
        if shape_type == "start":
            if connector_cfg is not None:
                conn_type = connector_cfg.get("connectorType", "")
                op_id = connector_cfg.get("operationId", "")
                if conn_type == "wss":
                    trigger = extract_wss_trigger(op_id, component_index)
                else:
                    trigger = {
                        "source_tag": f"{conn_type}:listen",
                        "type": "connector_trigger",
                        "label": userlabel,
                        "connector_type": conn_type,
                        "operation_id": op_id,
                        "requires_review": True,
                        "note": f"Non-WSS trigger ({conn_type}) — manual mapping required",
                    }

        # ── Return Documents ───────────────────────────────────────────────
        elif shape_type == "returndocuments":
            # Not a step — it's the process response endpoint; implicitly handled
            pass

        # ── DB / REST Connector Action ─────────────────────────────────────
        elif shape_type == "connectoraction" and connector_cfg is not None:
            conn_type = connector_cfg.get("connectorType", "")
            op_id = connector_cfg.get("operationId", "")
            conn_id = connector_cfg.get("connectionId", "")

            if "dbv2" in conn_type or "db" in conn_type.lower():
                seq += 1
                step = extract_db_step(shape, op_id, conn_id, component_index, seq)
                step["label"] = userlabel or step["label"]
                connections_used.add(conn_id)
                steps.append(step)
            elif "rest" in conn_type.lower() or conn_type == "http":
                seq += 1
                steps.append({
                    "source_tag": f"{conn_type}:request",
                    "type": "http_request",
                    "label": userlabel,
                    "config_ref": conn_id,
                    "requires_review": True,
                    "boomi_step": "REST_Connector",
                    "complexity": "medium",
                    "operation_id": op_id,
                    "connection_id": conn_id,
                    "note": "REST connector step — check operation XML for HTTP method and URL",
                    "sequence": seq,
                })
                connections_used.add(conn_id)
            else:
                # Unknown connector type — flag for review
                seq += 1
                steps.append({
                    "source_tag": f"{conn_type}:action",
                    "type": "connector_action",
                    "label": userlabel,
                    "connector_type": conn_type,
                    "operation_id": op_id,
                    "connection_id": conn_id,
                    "requires_review": True,
                    "note": f"Unknown connector type: {conn_type}",
                    "sequence": seq,
                })

        # ── Map ───────────────────────────────────────────────────────────
        elif shape_type == "map":
            seq += 1
            step = extract_map_step(shape, seq)
            step["label"] = userlabel or step["label"]
            steps.append(step)

        # ── Message ───────────────────────────────────────────────────────
        elif shape_type == "message":
            seq += 1
            step = extract_message_step(shape, seq)
            step["label"] = userlabel or step["label"]
            steps.append(step)

        # ── Set Properties ────────────────────────────────────────────────
        elif shape_type == "documentproperties":
            seq += 1
            step = extract_set_properties_step(shape, seq)
            step["label"] = userlabel or step["label"]
            steps.append(step)

        # ── Data Process (Groovy) ─────────────────────────────────────────
        elif shape_type == "dataprocess":
            seq += 1
            step = extract_groovy_step(shape, seq)
            step["label"] = userlabel or step["label"]
            steps.append(step)

        # ── Decision / Route / Branch ─────────────────────────────────────
        elif shape_type in ("decision", "route", "branch"):
            seq += 1
            steps.append({
                "source_tag": f"boomi:{shape_type}",
                "type": SHAPE_TYPE_LABELS.get(shape_type, shape_type),
                "label": userlabel,
                "config_ref": None,
                "requires_review": True,
                "boomi_step": shape_type.capitalize(),
                "complexity": "medium",
                "note": f"Boomi {shape_type} shape — review branching logic for target platform",
                "sequence": seq,
            })

        # ── Process Call ──────────────────────────────────────────────────
        elif shape_type == "processcall":
            seq += 1
            called_id = shape.find("configuration/processcall")
            called_id = called_id.get("processId") if called_id is not None else ""
            steps.append({
                "source_tag": "boomi:processcall",
                "type": "subprocess_call",
                "label": userlabel,
                "config_ref": None,
                "requires_review": True,
                "boomi_step": "Process_Call",
                "complexity": "medium",
                "called_process_id": called_id,
                "note": "Subprocess call — ensure called process is also migrated",
                "sequence": seq,
            })

        # ── Other / Unknown ───────────────────────────────────────────────
        elif shape_type not in ("start",):
            seq += 1
            steps.append({
                "source_tag": f"boomi:{shape_type}",
                "type": SHAPE_TYPE_LABELS.get(shape_type, "unknown"),
                "label": userlabel,
                "config_ref": None,
                "requires_review": True,
                "boomi_step": shape_type,
                "complexity": "low",
                "note": f"Unknown shape type: {shape_type}",
                "sequence": seq,
            })

    return {
        "name": name,
        "source_name": name,
        "component_id": component_id,
        "description": description,
        "flow_type": "primary",
        "trigger": trigger or {"type": "unknown", "requires_review": True},
        "steps": steps,
        "connections_used": list(connections_used),
        "error_handling": {
            "has_error_handler": False,
            "strategies": [],
            "note": "Boomi error handling (Try/Catch shapes) not yet captured — review process for exception paths",
        },
        "boomi_suggestions": {
            "process_name": name,
            "pattern": "request_reply_api" if trigger and trigger.get("type") == "http_listener" else "unknown",
        },
    }


def analyze_directory(directory, project_name=None, output_path=None):
    """Analyze all Boomi processes in a directory and produce a migration spec."""
    directory = os.path.abspath(directory)

    if not os.path.isdir(directory):
        print(f"ERROR: {directory} is not a directory", file=sys.stderr)
        sys.exit(1)

    print(f"Building component index from {directory}...")
    component_index = build_component_index(directory)
    print(f"  Indexed {len(component_index)} Boomi components")

    process_dir = os.path.join(directory, "process")
    if not os.path.isdir(process_dir):
        print(f"WARNING: No 'process/' subdirectory found. Scanning root for process XMLs.")
        process_dir = directory

    process_files = sorted([
        os.path.join(process_dir, f)
        for f in os.listdir(process_dir)
        if f.endswith(".xml")
    ])
    print(f"  Found {len(process_files)} process files")

    flows = []
    all_connection_ids = set()

    for pf in process_files:
        print(f"  Analyzing {os.path.basename(pf)}...")
        try:
            flow = analyze_process(pf, component_index)
            flows.append(flow)
            all_connection_ids.update(flow.get("connections_used", []))
        except Exception as e:
            print(f"    WARNING: {e}", file=sys.stderr)

    # Resolve connections
    connections = {}
    for conn_id in all_connection_ids:
        info = extract_connection_info(conn_id, component_index)
        key = info.get("name") or conn_id
        connections[key] = info

    # Collect gaps
    gaps = []
    for flow in flows:
        if flow["trigger"] and flow["trigger"].get("requires_review"):
            gaps.append({"flow": flow["name"], "component": "trigger", "note": flow["trigger"].get("note", "")})
        for step in flow.get("steps", []):
            if step.get("requires_review"):
                gaps.append({
                    "flow": flow["name"],
                    "step_type": step.get("type"),
                    "sequence": step.get("sequence"),
                    "label": step.get("label", ""),
                    "note": step.get("note", "Requires manual review"),
                })

    if not project_name:
        project_name = Path(directory).name

    spec = {
        "schema_version": "1.0",
        "source_system": "boomi",
        "source_version": "boomi_integration_platform",
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "project_name": project_name,
        "source_files_analyzed": [os.path.relpath(pf) for pf in process_files],
        "summary": {
            "total_flows": len(flows),
            "primary_flows": len(flows),
            "sub_flows": 0,
            "total_connections": len(connections),
            "gaps_found": len(gaps),
            "overall_complexity": "high" if any(g.get("step_type") == "custom_script" for g in gaps) else "medium",
        },
        "connections": connections,
        "integrations": flows,
        "gaps": gaps,
        "migration_notes": "",
    }

    if not output_path:
        os.makedirs("migration-specs", exist_ok=True)
        output_path = os.path.join("migration-specs", f"{project_name}.json")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)

    print(f"\nSpec written to: {output_path}")
    print(f"  Flows:       {len(flows)}")
    print(f"  Connections: {len(connections)}")
    print(f"  Gaps:        {len(gaps)}")

    return spec


def main():
    parser = argparse.ArgumentParser(
        description="Analyze a Boomi active-development directory into a migration spec JSON."
    )
    parser.add_argument(
        "directory",
        help="Path to Boomi active-development/ directory (must contain a process/ subdirectory)",
    )
    parser.add_argument("--output", help="Output file path (default: migration-specs/<project>.json)")
    parser.add_argument("--project", help="Project name (default: directory name)")
    args = parser.parse_args()

    analyze_directory(args.directory, project_name=args.project, output_path=args.output)


if __name__ == "__main__":
    main()
