#!/usr/bin/env python3
"""
Celigo Recipe Generator (Spec-Driven)
Reads any platform-agnostic migration spec and creates Celigo flows via the integrator.io API.

Usage:
    python generators/generate_celigo.py migration-specs/boomi-customer-api.json
    python generators/generate_celigo.py migration-specs/boomi-customer-api.json --integration "MIG_CustomerAPI"
    python generators/generate_celigo.py migration-specs/boomi-customer-api.json --dry-run

Required environment variables:
    CELIGO_API_TOKEN   Bearer token from integrator.io → Settings → API tokens
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime


CELIGO_BASE_URL = "https://api.integrator.io/v1"


class CeligoClient:
    def __init__(self, api_token):
        try:
            import requests
            self._req = requests
        except ImportError:
            print("ERROR: pip install requests", file=sys.stderr)
            sys.exit(1)
        self.session = self._req.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _get(self, path, params=None):
        r = self.session.get(f"{CELIGO_BASE_URL}{path}", params=params)
        if not r.ok:
            raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _post(self, path, body):
        r = self.session.post(f"{CELIGO_BASE_URL}{path}", json=body)
        if not r.ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def list_integrations(self):
        return self._get("/integrations")

    def create_integration(self, name, description=""):
        return self._post("/integrations", {"name": name, "description": description})

    def find_or_create_integration(self, name):
        for intg in self.list_integrations():
            if intg.get("name") == name:
                return intg["_id"]
        result = self.create_integration(name)
        return result["_id"]

    def create_export(self, name, adaptor_type, config):
        """Create a Celigo export (source connector)."""
        body = {
            "name": name,
            "adaptorType": adaptor_type,
            **config,
        }
        return self._post("/exports", body)

    def create_import(self, name, adaptor_type, config):
        """Create a Celigo import (destination connector)."""
        body = {
            "name": name,
            "adaptorType": adaptor_type,
            **config,
        }
        return self._post("/imports", body)

    def create_flow(self, name, integration_id, page_generators, page_processors, description=""):
        """Create a Celigo flow linking exports → processors → imports."""
        body = {
            "name": name,
            "_integrationId": integration_id,
            "description": description,
            "pageGenerators": page_generators,
            "pageProcessors": page_processors,
        }
        return self._post("/flows", body)


# ─── Flow Builder ─────────────────────────────────────────────────────────────

class CeligoFlowBuilder:
    """Translates a canonical flow spec into Celigo flow components."""

    def __init__(self, flow, spec):
        self.flow = flow
        self.spec = spec
        self.notes = []

    def build(self):
        """Returns (page_generators, page_processors, notes)."""
        pg = self._build_trigger()
        pps = self._build_processors()
        return pg, pps, self.notes

    def _build_trigger(self):
        t = self.flow.get("trigger", {})
        ttype = t.get("type", "")

        if ttype == "http_listener":
            return [{
                "type": "export",
                "adaptorType": "RESTExport",
                "_exportId": None,
                "_note": f"HTTP {t.get('http_method','GET')} {t.get('path','/endpoint')} — create REST export in Celigo",
            }]
        if ttype == "scheduler":
            return [{
                "type": "export",
                "adaptorType": "SimpleExport",
                "_note": "Scheduled trigger — configure schedule in Celigo flow settings",
            }]
        self.notes.append(f"Trigger type '{ttype}' requires manual setup in Celigo")
        return [{"type": "export", "adaptorType": "SimpleExport", "_note": f"Review trigger: {ttype}"}]

    def _build_processors(self):
        pps = []
        for step in self.flow.get("steps", []):
            stype = step.get("type", "")

            if stype in ("db_select", "db_insert", "db_update", "db_delete"):
                pps.append(self._db_processor(step))
            elif stype == "set_payload":
                pps.append({
                    "type": "import",
                    "adaptorType": "RESTImport",
                    "_note": f"Static response: {step.get('static_content','')[:80]}",
                })
            elif stype == "transform":
                pps.append({
                    "type": "transform",
                    "_note": f"Field mapping step: '{step.get('label','')}' — configure in Celigo Mapper 2.0",
                })
            elif stype == "custom_script":
                note = f"Custom script '{step.get('label','')}' — implement in Celigo using a JavaScript hook"
                self.notes.append(note)
                pps.append({"type": "transform", "_hookNote": note})
            elif stype == "choice_router":
                note = f"Conditional routing '{step.get('label','')}' — implement as Celigo branching"
                self.notes.append(note)
                pps.append({"type": "branching", "_note": note})
            elif stype in ("return_response", "logger"):
                pass  # Celigo handles these implicitly
            else:
                note = f"Step type '{stype}' not auto-mapped for Celigo"
                self.notes.append(note)
                pps.append({"type": "transform", "_note": note})

        return pps

    def _db_processor(self, step):
        sql = step.get("sql", "")
        table = step.get("table", "table")
        stype = step.get("type", "db_select")
        op_map = {"db_select": "SELECT", "db_insert": "INSERT", "db_update": "UPDATE", "db_delete": "DELETE"}
        return {
            "type": "import",
            "adaptorType": "RDBMSImport",
            "rdbms": {
                "query": sql or f"{op_map.get(stype,'SELECT')} * FROM {table}",
                "queryType": op_map.get(stype, "SELECT"),
            },
            "_note": f"{stype} on table '{table}'",
        }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate Celigo flows from a migration spec.")
    parser.add_argument("spec", nargs="?", default="migration-specs/boomi-customer-api.json")
    parser.add_argument("--integration", default=None,
                        help="Celigo integration name (default: MIG_<project>)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.isfile(args.spec):
        print(f"ERROR: Spec not found: {args.spec}", file=sys.stderr)
        sys.exit(1)

    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)

    project_name = spec.get("project_name", "MigrationProject")
    integration_name = args.integration or f"MIG_{project_name}"
    flows = spec.get("integrations", [])

    print(f"Celigo Generator")
    print(f"  Source  : {spec.get('source_system')}")
    print(f"  Project : {project_name}")
    print(f"  Integration: {integration_name}")
    print(f"  Flows   : {len(flows)}")

    if args.dry_run:
        print("\n=== DRY RUN ===\n")
        for flow in flows:
            builder = CeligoFlowBuilder(flow, spec)
            pg, pps, notes = builder.build()
            fname = flow["name"] if flow["name"].upper().startswith("MIG_") else f"MIG_{flow['name']}"
            print(f"Flow: {fname}")
            print(f"  Page generators: {json.dumps(pg, indent=4)}")
            print(f"  Page processors: {json.dumps(pps, indent=4)}")
            if notes:
                print(f"  Manual review ({len(notes)} items):")
                for n in notes:
                    print(f"    - {n[:100]}")
            print("-" * 60)
        return

    api_token = os.environ.get("CELIGO_API_TOKEN")
    if not api_token:
        print("ERROR: CELIGO_API_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    client = CeligoClient(api_token)

    print(f"\nEnsuring Celigo integration '{integration_name}'...")
    integration_id = client.find_or_create_integration(integration_name)
    print(f"  Integration id: {integration_id}")

    results = []
    all_notes = []
    for flow in flows:
        flow_name = flow["name"]
        fname = flow_name if flow_name.upper().startswith("MIG_") else f"MIG_{flow_name}"
        print(f"  Creating flow '{fname}'...")

        builder = CeligoFlowBuilder(flow, spec)
        pg, pps, notes = builder.build()
        all_notes.extend([(fname, n) for n in notes])

        try:
            result = client.create_flow(
                name=fname,
                integration_id=integration_id,
                page_generators=pg,
                page_processors=pps,
                description=f"Migrated from {spec.get('source_system')}. Generated {datetime.utcnow().isoformat()}Z",
            )
            fid = result.get("_id")
            print(f"    Created (id: {fid})")
            results.append({"name": fname, "id": fid, "status": "created"})
            time.sleep(0.5)
        except Exception as e:
            print(f"    FAILED: {e}", file=sys.stderr)
            results.append({"name": fname, "status": "failed", "error": str(e)})

    output_path = args.spec.replace(".json", "_celigo_output.json")
    with open(output_path, "w") as f:
        json.dump({
            "integration_name": integration_name,
            "integration_id": integration_id,
            "flows": results,
            "manual_review": [{"flow": n, "note": note} for n, note in all_notes],
        }, f, indent=2)

    created = sum(1 for r in results if r["status"] == "created")
    print(f"\nDone. {created}/{len(flows)} flows created. Output: {output_path}")


if __name__ == "__main__":
    main()
