#!/usr/bin/env python3
"""
webMethods.io Flow Generator (Spec-Driven)
Reads any platform-agnostic migration spec and creates webMethods.io workflows
via the webMethods.io REST API, or writes local JSON files for manual import.

Usage:
    python generators/generate_webmethods.py migration-specs/boomi-customer-api.json
    python generators/generate_webmethods.py migration-specs/boomi-customer-api.json --project "MIG_CustomerAPI"
    python generators/generate_webmethods.py migration-specs/boomi-customer-api.json --dry-run

Required environment variables (live push):
    WMIO_TENANT_URL     e.g. https://mycompany.int-aws-us.webmethods.io
    WMIO_USERNAME       webMethods.io username
    WMIO_PASSWORD       webMethods.io password
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime


# ─── webMethods.io API Client ─────────────────────────────────────────────────

class WmioClient:
    def __init__(self, tenant_url, username, password):
        try:
            import requests
            self._req = requests
        except ImportError:
            print("ERROR: pip install requests", file=sys.stderr)
            sys.exit(1)

        self.base_url = tenant_url.rstrip("/")
        self.session = self._req.Session()
        self.session.auth = (username, password)
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _get(self, path, params=None):
        r = self.session.get(f"{self.base_url}{path}", params=params)
        if not r.ok:
            raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _post(self, path, body):
        r = self.session.post(f"{self.base_url}{path}", json=body)
        if not r.ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def list_projects(self):
        return self._get("/apis/v1/projects")

    def create_workflow(self, project_id, workflow_body):
        """Create a workflow inside a project."""
        return self._post(f"/apis/v1/projects/{project_id}/workflows", workflow_body)

    def find_or_create_project(self, project_name):
        """Find project by name or create it."""
        result = self.list_projects()
        projects = result.get("output", result) if isinstance(result, dict) else result
        for p in (projects if isinstance(projects, list) else []):
            if p.get("name") == project_name:
                return p.get("id") or p.get("uid")
        created = self._post("/apis/v1/projects", {"name": project_name})
        output = created.get("output", created)
        return output.get("id") or output.get("uid")


# ─── Workflow Builder ─────────────────────────────────────────────────────────

def _uid():
    return str(uuid.uuid4()).replace("-", "")[:16]


class WmioFlowBuilder:
    """
    Translates a canonical flow spec into a webMethods.io workflow definition.

    webMethods.io uses a node-edge graph model:
      - nodes: array of trigger/action nodes, each with a unique uid
      - edges: directed connections between node uids
    """

    def __init__(self, flow, spec):
        self.flow = flow
        self.spec = spec
        self.notes = []

    def build(self):
        """Returns (workflow_dict, notes)."""
        nodes = []
        edges = []

        trigger_node = self._build_trigger_node()
        nodes.append(trigger_node)

        step_nodes = self._build_step_nodes()
        nodes.extend(step_nodes)

        # Wire nodes sequentially
        for i in range(len(nodes) - 1):
            edges.append({
                "from": nodes[i]["uid"],
                "to": nodes[i + 1]["uid"],
            })

        name = self.flow.get("name", "workflow")
        if not name.upper().startswith("MIG_"):
            name = f"MIG_{name}"

        workflow = {
            "name": name,
            "description": f"Migrated from {self.spec.get('source_system', 'source')}. "
                           f"Generated {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "nodes": nodes,
            "edges": edges,
        }
        return workflow, self.notes

    # ── Trigger ───────────────────────────────────────────────────────────────

    def _build_trigger_node(self):
        t = self.flow.get("trigger", {})
        ttype = t.get("type", "")
        uid = _uid()

        if ttype == "http_listener":
            method = t.get("http_method", "POST").upper()
            path = t.get("path", "/endpoint")
            return {
                "uid": uid,
                "type": "trigger",
                "name": "receive_http_request",
                "label": t.get("label", "HTTP Trigger"),
                "configuration": {
                    "method": method,
                    "path": path,
                    "headers": {},
                },
            }

        if ttype == "scheduler":
            schedule = t.get("schedule", {})
            return {
                "uid": uid,
                "type": "trigger",
                "name": "scheduler",
                "label": "Scheduled Trigger",
                "configuration": schedule,
            }

        self.notes.append(f"Trigger type '{ttype}' mapped to manual trigger — configure in wmio")
        return {
            "uid": uid,
            "type": "trigger",
            "name": "manual",
            "label": t.get("label", f"Manual ({ttype})"),
            "configuration": {},
        }

    # ── Steps ─────────────────────────────────────────────────────────────────

    def _build_step_nodes(self):
        nodes = []
        for step in self.flow.get("steps", []):
            node = self._step_to_node(step)
            if node:
                nodes.append(node)
        return nodes

    def _step_to_node(self, step):
        stype = step.get("type", "")
        label = step.get("label", stype)
        uid = _uid()

        if stype in ("db_select", "db_insert", "db_update", "db_delete"):
            return self._db_node(uid, step)

        if stype == "http_request":
            method = step.get("http_method", "GET").upper()
            return {
                "uid": uid,
                "type": "action",
                "name": "http_request",
                "label": label,
                "configuration": {
                    "method": method,
                    "url": step.get("url", ""),
                    "headers": {},
                },
            }

        if stype == "transform":
            return {
                "uid": uid,
                "type": "action",
                "name": "transform",
                "label": label,
                "configuration": {
                    "_note": "Configure field mapping in webMethods.io Transform step",
                },
            }

        if stype == "choice_router":
            self.notes.append(f"Conditional routing '{label}' — implement as wmio branch/condition node")
            return {
                "uid": uid,
                "type": "action",
                "name": "condition",
                "label": label,
                "configuration": {
                    "_note": "Configure condition expression in webMethods.io",
                },
            }

        if stype == "custom_script":
            self.notes.append(f"Custom script '{label}' — implement as wmio custom action (JavaScript)")
            return {
                "uid": uid,
                "type": "action",
                "name": "custom_action",
                "label": label,
                "configuration": {
                    "_note": "Implement in webMethods.io custom action editor",
                },
            }

        if stype in ("return_response", "set_payload"):
            body = step.get("static_content", "")
            return {
                "uid": uid,
                "type": "action",
                "name": "set_variable",
                "label": label,
                "configuration": {
                    "content": body[:200] if body else "",
                    "_note": "Configure response payload in webMethods.io",
                },
            }

        if stype in ("sequence", "branch", "loop", "subprocess_call", "logger"):
            self.notes.append(f"Step type '{stype}' ('{label}') requires manual wmio mapping")
            return {
                "uid": uid,
                "type": "action",
                "name": "custom_action",
                "label": f"[REVIEW] {label}",
                "configuration": {"_note": f"Source step type: {stype}"},
            }

        # Catch-all
        self.notes.append(f"Step type '{stype}' not auto-mapped for webMethods.io")
        return {
            "uid": uid,
            "type": "action",
            "name": "custom_action",
            "label": f"[REVIEW] {label}",
            "configuration": {"_note": f"Not auto-mapped: {stype}"},
        }

    def _db_node(self, uid, step):
        stype = step.get("type", "db_select")
        table = step.get("table", "table")
        sql = step.get("sql", "")
        label = step.get("label", stype)

        op_map = {
            "db_select": "SELECT",
            "db_insert": "INSERT",
            "db_update": "UPDATE",
            "db_delete": "DELETE",
        }
        wmio_name_map = {
            "db_select": "select_rows",
            "db_insert": "insert_row",
            "db_update": "update_row",
            "db_delete": "delete_row",
        }
        return {
            "uid": uid,
            "type": "action",
            "name": wmio_name_map.get(stype, "select_rows"),
            "label": label,
            "configuration": {
                "table": table,
                "sql": sql or f"{op_map.get(stype,'SELECT')} * FROM {table}",
                "_note": f"Configure database connection in webMethods.io connector",
            },
        }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate webMethods.io workflows from a migration spec.")
    parser.add_argument("spec", nargs="?", default="migration-specs/boomi-customer-api.json")
    parser.add_argument("--project", default=None,
                        help="webMethods.io project name (default: MIG_<source_project>)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write workflow JSON locally without pushing to wmio")
    parser.add_argument("--output-dir", default="wmio-output",
                        help="Local output directory for dry-run JSON files (default: wmio-output)")
    args = parser.parse_args()

    if not os.path.isfile(args.spec):
        print(f"ERROR: Spec not found: {args.spec}", file=sys.stderr)
        sys.exit(1)

    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)

    project_name = args.project or f"MIG_{spec.get('project_name', 'MigrationProject')}"
    flows = spec.get("integrations", [])

    print("webMethods.io Generator")
    print(f"  Source  : {spec.get('source_system')}")
    print(f"  Project : {project_name}")
    print(f"  Flows   : {len(flows)}")

    workflows = []
    all_notes = []

    for flow in flows:
        builder = WmioFlowBuilder(flow, spec)
        workflow, notes = builder.build()
        workflows.append(workflow)
        all_notes.extend([(workflow["name"], n) for n in notes])

    # ── Dry-run: write to local files ─────────────────────────────────────────
    if args.dry_run:
        print(f"\n=== DRY RUN — writing to {args.output_dir}/ ===\n")
        os.makedirs(args.output_dir, exist_ok=True)
        for wf in workflows:
            fname = f"{wf['name'].replace('/', '_')}.json"
            fpath = os.path.join(args.output_dir, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(wf, f, indent=2)
            print(f"  Written: {fpath}")
        if all_notes:
            print(f"\nManual review ({len(all_notes)} items):")
            for name, note in all_notes:
                print(f"  [{name}] {note[:100]}")
        return

    # ── Live push ─────────────────────────────────────────────────────────────
    tenant_url = os.environ.get("WMIO_TENANT_URL")
    username = os.environ.get("WMIO_USERNAME")
    password = os.environ.get("WMIO_PASSWORD")

    if not all([tenant_url, username, password]):
        missing = [v for v, k in [
            ("WMIO_TENANT_URL", tenant_url),
            ("WMIO_USERNAME", username),
            ("WMIO_PASSWORD", password),
        ] if not k]
        print(f"ERROR: Missing env vars: {', '.join(missing)}", file=sys.stderr)
        print("Set WMIO_TENANT_URL, WMIO_USERNAME, WMIO_PASSWORD or use --dry-run", file=sys.stderr)
        sys.exit(1)

    client = WmioClient(tenant_url, username, password)

    print(f"\nEnsuring webMethods.io project '{project_name}'...")
    try:
        project_id = client.find_or_create_project(project_name)
        print(f"  Project id: {project_id}")
    except Exception as e:
        print(f"ERROR finding/creating project: {e}", file=sys.stderr)
        sys.exit(1)

    results = []
    for wf in workflows:
        wf_name = wf["name"]
        print(f"  Creating workflow '{wf_name}'...")
        try:
            result = client.create_workflow(project_id, wf)
            wf_id = result.get("output", result).get("uid") or result.get("id")
            print(f"    Created (id: {wf_id})")
            results.append({"name": wf_name, "id": wf_id, "status": "created"})
        except Exception as e:
            print(f"    FAILED: {e}", file=sys.stderr)
            results.append({"name": wf_name, "status": "failed", "error": str(e)})

    output_path = args.spec.replace(".json", "_wmio_output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "project_name": project_name,
            "project_id": project_id,
            "workflows": results,
            "manual_review": [{"workflow": n, "note": note} for n, note in all_notes],
        }, f, indent=2)

    created = sum(1 for r in results if r["status"] == "created")
    print(f"\nDone. {created}/{len(flows)} workflows created. Output: {output_path}")


if __name__ == "__main__":
    main()
