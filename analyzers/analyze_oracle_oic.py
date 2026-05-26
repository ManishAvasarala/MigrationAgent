#!/usr/bin/env python3
"""
Oracle Integration Cloud (OIC) → Boomi Migration Analyzer

Pulls integrations from a live OIC instance via its REST Management API v3,
parses trigger/invoke/mapping structure, and produces a normalized migration
spec JSON.

Supported artifact sources (checked in priority order):
  1. Live OIC REST API  (--oic-host / env vars)
  2. Local .iar export files (--source-dir with .iar / .zip files)

Usage:
  # Pull from live OIC and analyze all integrations
  python analyzers/analyze_oracle_oic.py --project my-project

  # Analyze only integrations whose name matches a pattern
  python analyzers/analyze_oracle_oic.py --filter "Customer*" --project customers

  # Use locally exported .iar files (no live system needed)
  python analyzers/analyze_oracle_oic.py --source-dir /path/to/iars/ --project my-project

  # Write spec to a custom path
  python analyzers/analyze_oracle_oic.py --project my-project --output migration-specs/my-project.json

Environment variables (for live pull):
  ORACLE_OIC_HOST       OIC instance hostname (e.g. mycompany.integration.ocp.oraclecloud.com)
  ORACLE_OIC_PORT       Port (default: 443)
  ORACLE_OIC_USERNAME   Oracle Cloud IAM username / email
  ORACLE_OIC_PASSWORD   Password
  ORACLE_OIC_VERSION    API version (default: v3; v2 also supported)

Output:
  migration-specs/<project>.json
"""

import sys
import os
import re
import json
import zipfile
import argparse
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


# ---------------------------------------------------------------------------
# Adapter → canonical connection type mapping
# ---------------------------------------------------------------------------

OIC_ADAPTER_MAP: Dict[str, Dict] = {
    # REST / HTTP
    "REST":                {"type": "http_request",   "boomi": "rest_connection",           "driver": "rest"},
    "SOAP":                {"type": "http_request",   "boomi": "rest_connection",            "driver": "soap"},
    "HTTP":                {"type": "http_request",   "boomi": "rest_connection",            "driver": "http"},
    # Database
    "ORACLE_DB":           {"type": "db",             "boomi": "databasev2_connection",      "driver": "oracle"},
    "ORACLE_ATP":          {"type": "db",             "boomi": "databasev2_connection",      "driver": "oracle_atp"},
    "ORACLE_ADWC":         {"type": "db",             "boomi": "databasev2_connection",      "driver": "oracle_adwc"},
    "DB":                  {"type": "db",             "boomi": "databasev2_connection",      "driver": "oracle"},
    "MYSQL":               {"type": "db",             "boomi": "databasev2_connection",      "driver": "mysql"},
    "MSSQL":               {"type": "db",             "boomi": "databasev2_connection",      "driver": "sqlserver"},
    # Oracle EBS / Fusion
    "EBS":                 {"type": "oracle_ebs",     "boomi": "oracle_ebs_connection",      "driver": "ebs"},
    "ORACLE_EBS":          {"type": "oracle_ebs",     "boomi": "oracle_ebs_connection",      "driver": "ebs"},
    "ORACLE_FUSION_APPS":  {"type": "oracle_ebs",     "boomi": "rest_connection",            "driver": "oracle_fusion"},
    "ORACLE_HCM_CLOUD":    {"type": "oracle_ebs",     "boomi": "rest_connection",            "driver": "oracle_hcm"},
    "ORACLE_ERP_CLOUD":    {"type": "oracle_ebs",     "boomi": "rest_connection",            "driver": "oracle_erp"},
    "ORACLE_CX_SALES":     {"type": "http_request",   "boomi": "rest_connection",            "driver": "oracle_cx"},
    # File / FTP
    "FTP":                 {"type": "sftp",           "boomi": "diskv2_connection",          "driver": "ftp"},
    "SFTP":                {"type": "sftp",           "boomi": "diskv2_connection",          "driver": "sftp"},
    "FILE":                {"type": "file",           "boomi": "diskv2_connection",          "driver": "local"},
    # Messaging / Events
    "JMS":                 {"type": "jms",            "boomi": "event_streams_connection",   "driver": "jms"},
    "AQ":                  {"type": "oracle_aq",      "boomi": "event_streams_connection",   "driver": "aq"},
    "KAFKA":               {"type": "jms",            "boomi": "event_streams_connection",   "driver": "kafka"},
    # CRM / SaaS
    "SALESFORCE":          {"type": "salesforce",     "boomi": "salesforce_connection",      "driver": "salesforce"},
    "SERVICENOW":          {"type": "http_request",   "boomi": "rest_connection",            "driver": "servicenow"},
    "ELOQUA":              {"type": "http_request",   "boomi": "rest_connection",            "driver": "eloqua"},
    "NETSUITE":            {"type": "custom",         "boomi": "netsuite_connection",        "driver": "netsuite"},
    "ORACLE_SERVICE_CLOUD":{"type": "http_request",   "boomi": "rest_connection",            "driver": "osc"},
    # ERP
    "SAP":                 {"type": "custom",         "boomi": "boomi_for_sap_connection",   "driver": "sap"},
    # B2B / EDI
    "B2B":                 {"type": "b2b",            "boomi": "trading_partner",            "driver": "b2b"},
    "EDI_MAPPER":          {"type": "b2b",            "boomi": "trading_partner",            "driver": "edi"},
    # Storage
    "ORACLE_STORAGE_CLOUD":{"type": "file",           "boomi": "rest_connection",            "driver": "oss"},
    "AMAZON_S3":           {"type": "file",           "boomi": "rest_connection",            "driver": "s3"},
}

# OIC integration style → canonical pattern
OIC_STYLE_MAP: Dict[str, str] = {
    "ORCHESTRATION":               "orchestration",
    "APP_DRIVEN_ORCHESTRATION":    "orchestration",
    "SCHEDULED_ORCHESTRATION":     "scheduled_batch",
    "BASIC_ROUTING":               "pass_through",
    "PUBLISH_TO_OIC":              "event_driven",
    "SUBSCRIBE_FROM_OIC":          "event_driven",
    "FILE_TRANSFER":               "file_processing",
}

# OIC trigger adapter → canonical trigger type
OIC_TRIGGER_TYPE_MAP: Dict[str, str] = {
    "REST":                "http_listener",
    "SOAP":                "http_listener",
    "HTTP":                "http_listener",
    "FTP":                 "sftp_listener",
    "SFTP":                "sftp_listener",
    "FILE":                "file_listener",
    "JMS":                 "jms_listener",
    "AQ":                  "jms_listener",
    "KAFKA":               "jms_listener",
    "SCHEDULE":            "scheduler_fixed",
}

# OIC invoke adapter → canonical step type (default to http_request if unknown)
OIC_INVOKE_STEP_MAP: Dict[str, str] = {
    "REST":                "http_request",
    "SOAP":                "http_request",
    "HTTP":                "http_request",
    "ORACLE_DB":           "db_select",
    "ORACLE_ATP":          "db_select",
    "ORACLE_ADWC":         "db_select",
    "DB":                  "db_select",
    "EBS":                 "oracle_ebs_api",
    "ORACLE_EBS":          "oracle_ebs_api",
    "ORACLE_FUSION_APPS":  "http_request",
    "ORACLE_HCM_CLOUD":    "http_request",
    "ORACLE_ERP_CLOUD":    "http_request",
    "FTP":                 "sftp_write",
    "SFTP":                "sftp_write",
    "FILE":                "file_write",
    "JMS":                 "jms_publish",
    "AQ":                  "jms_publish",
    "KAFKA":               "jms_publish",
    "SALESFORCE":          "salesforce_create",
    "SERVICENOW":          "http_request",
    "SAP":                 "custom",
    "NETSUITE":            "http_request",
    "B2B":                 "b2b_send",
}

# Boomi step suggestion per step type
BOOMI_STEP_MAP: Dict[str, str] = {
    "http_listener":   "WSS_Listener",
    "http_request":    "REST_Connector",
    "db_select":       "DatabaseV2_Connector_GET",
    "db_insert":       "DatabaseV2_Connector_INSERT",
    "db_update":       "DatabaseV2_Connector_UPDATE",
    "oracle_ebs_api":  "REST_Connector_or_Oracle_EBS_Connector",
    "sftp_write":      "Disk_V2_CREATE",
    "sftp_read":       "Disk_V2_GET",
    "file_write":      "Disk_V2_CREATE",
    "file_read":       "Disk_V2_GET",
    "jms_publish":     "Event_Streams_Produce",
    "salesforce_create":"Salesforce_Connector_CREATE",
    "transform":       "Map_Component",
    "set_variable":    "Set_Properties",
    "choice_router":   "Decision",
    "scatter_gather":  "Branch",
    "foreach":         "Data_Process_Split",
    "custom":          "Custom_Connector_or_REST",
    "b2b_send":        "Trading_Partner",
}


def _adapter_id(conn_obj: dict) -> str:
    """Extract adapterId from a connection dict, normalizing to uppercase."""
    raw = (
        conn_obj.get("adapterId")
        or conn_obj.get("adapter", {}).get("id", "")
        or ""
    )
    return raw.upper()


def _conn_name(conn_obj: dict) -> str:
    return conn_obj.get("name") or conn_obj.get("id") or "unknown"


# ---------------------------------------------------------------------------
# OIC REST API client
# ---------------------------------------------------------------------------

class OicApiClient:
    """Thin wrapper around OIC REST API v3."""

    def __init__(self, host: str, username: str, password: str,
                 port: int = 443, api_version: str = "v3"):
        self.base_url = f"https://{host}:{port}/ic/api/integration/{api_version}"
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        if not _HAS_REQUESTS:
            raise RuntimeError("requests library not installed. Run: pip install requests")
        url = f"{self.base_url}{path}"
        resp = requests.get(url, headers=self.headers, params=params, timeout=30, verify=True)
        resp.raise_for_status()
        return resp.json()

    def list_integrations(self, name_filter: str = None) -> List[dict]:
        """Fetch all integrations, handling OIC pagination."""
        items = []
        offset = 0
        limit = 100
        while True:
            params = {"limit": limit, "offset": offset, "expand": "references"}
            if name_filter:
                params["q"] = f"name:{name_filter}"
            data = self._get("/integrations", params=params)
            batch = data.get("items", [])
            items.extend(batch)
            if not data.get("hasMore", False) or not batch:
                break
            offset += limit
        return items

    def get_integration(self, integration_id: str) -> dict:
        """Fetch full detail for one integration."""
        # URL-encode the | in IDs like "MY_INT|01.00.0000"
        safe_id = integration_id.replace("|", "%7C")
        return self._get(f"/integrations/{safe_id}")


# ---------------------------------------------------------------------------
# .iar (Integration Archive) parser
# ---------------------------------------------------------------------------

class IarParser:
    """Parse OIC .iar export files (ZIP archives)."""

    def __init__(self, iar_path: str):
        self.path = iar_path
        self.name = Path(iar_path).stem

    def extract_metadata(self) -> Optional[dict]:
        """Return integration metadata dict or None if unrecognised format."""
        try:
            with zipfile.ZipFile(self.path, "r") as zf:
                names = zf.namelist()
                # OIC Gen2/Gen3 place metadata in these locations
                candidates = [
                    n for n in names
                    if n.endswith("integration.json") or n.endswith("metadata.json")
                ]
                if not candidates:
                    # Try top-level JSON files
                    candidates = [n for n in names if n.endswith(".json") and "/" not in n]
                if not candidates:
                    return None
                with zf.open(candidates[0]) as f:
                    return json.load(f)
        except Exception as e:
            print(f"  WARNING: Could not parse {self.path}: {e}")
            return None


# ---------------------------------------------------------------------------
# Integration → canonical spec mapper
# ---------------------------------------------------------------------------

class OicIntegrationMapper:
    """Maps one OIC integration dict to the canonical spec format."""

    def __init__(self, raw: dict, source_label: str = "oic"):
        self.raw = raw
        self.source_label = source_label

    # -- helpers -------------------------------------------------------------

    def _get_style(self) -> str:
        style = self.raw.get("style") or self.raw.get("pattern") or "ORCHESTRATION"
        return style.upper()

    def _get_primary_trigger_adapter(self) -> str:
        """Return the adapterId of the trigger connection."""
        # v3 structure
        trigger = self.raw.get("trigger") or {}
        conn = trigger.get("connection") or {}
        adapter = _adapter_id(conn)
        if adapter:
            return adapter

        # Alternative: primaryConnection with role TRIGGER
        pc = self.raw.get("primaryConnection") or {}
        if pc.get("role") == "TRIGGER":
            return _adapter_id(pc.get("connection") or {})

        # Scheduled integration
        if "SCHEDULED" in self._get_style():
            return "SCHEDULE"

        return "REST"

    def _get_invokes(self) -> List[dict]:
        """Return list of invoke connection dicts."""
        invokes = self.raw.get("invokes") or []
        if not invokes:
            # Some OIC versions use primaryConnection with role INVOKE
            pc = self.raw.get("primaryConnection") or {}
            if pc.get("role") == "INVOKE":
                invokes = [{"connection": pc.get("connection", {}), "invokeDetails": {}}]
        return invokes

    def _get_mappings(self) -> List[dict]:
        return self.raw.get("mappings") or []

    # -- trigger -------------------------------------------------------------

    def build_trigger(self) -> dict:
        adapter = self._get_primary_trigger_adapter()
        trigger_type = OIC_TRIGGER_TYPE_MAP.get(adapter, "http_listener")

        trigger = {
            "type": trigger_type,
            "config_ref": self.raw.get("trigger", {}).get("connection", {}).get("id", f"{adapter}_Connection"),
        }

        # REST/SOAP: try to extract resource path and method
        if adapter in ("REST", "SOAP", "HTTP"):
            td = self.raw.get("trigger", {}).get("triggerDetails") or {}
            trigger["path"] = td.get("resourcePath") or td.get("endpointPath") or "/api/resource"
            verbs = td.get("verbs") or td.get("methods") or ["POST"]
            trigger["method"] = verbs[0] if verbs else "POST"
            trigger["allowed_methods"] = verbs

        # Schedule: try to extract cron/interval
        elif adapter == "SCHEDULE":
            trigger["type"] = "scheduler_cron"
            sched = self.raw.get("schedule") or {}
            trigger["cron"] = sched.get("expression") or "0 0 * * *"
            trigger["timezone"] = sched.get("timezone") or "UTC"

        # FTP/SFTP: directory and pattern
        elif adapter in ("FTP", "SFTP", "FILE"):
            td = self.raw.get("trigger", {}).get("triggerDetails") or {}
            trigger["directory"] = td.get("directory") or td.get("inputDirectory") or "/inbound"
            trigger["file_pattern"] = td.get("fileNamePattern") or "*.*"

        return trigger

    # -- connections ---------------------------------------------------------

    def build_connections(self) -> Dict[str, dict]:
        connections: Dict[str, dict] = {}

        # Trigger connection
        trigger_conn = self.raw.get("trigger", {}).get("connection") or {}
        if trigger_conn:
            adapter = _adapter_id(trigger_conn)
            info = OIC_ADAPTER_MAP.get(adapter, {"type": "custom", "boomi": "rest_connection", "driver": "unknown"})
            name = _conn_name(trigger_conn)
            connections[name] = {
                "type": info["type"],
                "driver": info["driver"],
                "boomi_equivalent": info["boomi"],
                "adapter": adapter,
                "role": "trigger",
                "notes": f"OIC {adapter} adapter — configure credentials in Boomi GUI",
            }

        # Invoke connections
        for inv in self._get_invokes():
            conn = inv.get("connection") or {}
            if not conn:
                continue
            adapter = _adapter_id(conn)
            info = OIC_ADAPTER_MAP.get(adapter, {"type": "custom", "boomi": "rest_connection", "driver": "unknown"})
            name = _conn_name(conn)
            if name not in connections:
                connections[name] = {
                    "type": info["type"],
                    "driver": info["driver"],
                    "boomi_equivalent": info["boomi"],
                    "adapter": adapter,
                    "role": "invoke",
                    "notes": f"OIC {adapter} adapter — configure credentials in Boomi GUI",
                }

        return connections

    # -- steps ---------------------------------------------------------------

    def build_steps(self) -> List[dict]:
        steps = []
        seq = 1

        # Step 1: receive/trigger acknowledgement (implicit in OIC)
        trigger_adapter = self._get_primary_trigger_adapter()
        if trigger_adapter not in ("SCHEDULE", "FTP", "SFTP", "FILE"):
            steps.append({
                "sequence": seq,
                "type": "bpel_receive",
                "label": "Receive Request",
                "boomi_step": "WSS_Listener_or_Connector_Start",
                "requires_review": False,
            })
            seq += 1

        # Map steps (DataMapper transforms)
        mappings = self._get_mappings()
        if mappings:
            for i, m in enumerate(mappings):
                steps.append({
                    "sequence": seq,
                    "type": "transform",
                    "label": m.get("name") or f"Map {i + 1}",
                    "boomi_step": BOOMI_STEP_MAP["transform"],
                    "field_mappings": m.get("fields") or [],
                    "requires_review": False,
                })
                seq += 1
        elif self._get_style() not in ("BASIC_ROUTING", "FILE_TRANSFER"):
            # OIC always has at least one mapper, even if not returned by API listing
            steps.append({
                "sequence": seq,
                "type": "transform",
                "label": "Data Mapping",
                "boomi_step": BOOMI_STEP_MAP["transform"],
                "field_mappings": [],
                "requires_review": False,
                "notes": "OIC DataMapper — rebuild mapping in Boomi Map component",
            })
            seq += 1

        # Invoke steps
        for inv in self._get_invokes():
            conn = inv.get("connection") or {}
            adapter = _adapter_id(conn)
            details = inv.get("invokeDetails") or {}

            step_type = OIC_INVOKE_STEP_MAP.get(adapter, "http_request")

            # Refine DB step type based on operation
            if step_type == "db_select":
                op = (details.get("operation") or details.get("operationType") or "select").lower()
                if "insert" in op:
                    step_type = "db_insert"
                elif "update" in op or "upsert" in op or "merge" in op:
                    step_type = "db_update"
                elif "delete" in op:
                    step_type = "db_delete"
                elif "proc" in op or "function" in op:
                    step_type = "db_stored_procedure"

            # Refine Salesforce step type
            if adapter == "SALESFORCE":
                op = (details.get("operation") or "create").lower()
                if "query" in op or "select" in op:
                    step_type = "salesforce_query"
                elif "update" in op:
                    step_type = "salesforce_update"
                elif "upsert" in op:
                    step_type = "salesforce_upsert"

            boomi_step = BOOMI_STEP_MAP.get(step_type, "REST_Connector")
            requires_review = step_type in ("oracle_ebs_api", "custom", "b2b_send")

            step = {
                "sequence": seq,
                "type": step_type,
                "label": inv.get("name") or _conn_name(conn) or f"Invoke {seq}",
                "config_ref": _conn_name(conn),
                "boomi_step": boomi_step,
                "requires_review": requires_review,
            }

            # Add extra detail for known step types
            if step_type in ("db_select", "db_insert", "db_update", "db_stored_procedure"):
                step["sql"] = details.get("sqlStatement") or details.get("sql") or ""
                step["table"] = details.get("tableName") or details.get("businessObjectName") or ""

            elif step_type == "http_request":
                step["url"] = details.get("endpointUrl") or details.get("resourcePath") or ""
                step["method"] = details.get("httpMethod") or details.get("method") or "POST"

            elif step_type == "oracle_ebs_api":
                step["api_name"] = details.get("apiName") or details.get("businessObjectName") or ""
                step["notes"] = "Oracle EBS API call — check for native EBS connector in Boomi account"

            steps.append(step)
            seq += 1

        # Routing/switch steps
        routing = self.raw.get("routing") or self.raw.get("switch") or []
        if routing:
            steps.append({
                "sequence": seq,
                "type": "choice_router",
                "label": "Route",
                "boomi_step": BOOMI_STEP_MAP["choice_router"],
                "requires_review": False,
            })
            seq += 1

        # Reply step (for sync integrations)
        style = self._get_style()
        if trigger_adapter in ("REST", "SOAP", "HTTP") and "BASIC_ROUTING" not in style:
            steps.append({
                "sequence": seq,
                "type": "bpel_reply",
                "label": "Send Response",
                "boomi_step": "WSS_Return_Documents",
                "requires_review": False,
            })

        return steps

    # -- gaps ----------------------------------------------------------------

    def build_gaps(self, integration_name: str) -> List[dict]:
        gaps = []
        style = self._get_style()

        # Parallel execution in orchestration
        if "PARALLEL" in style or self.raw.get("parallelActivities"):
            gaps.append({
                "flow_name": integration_name,
                "step_sequence": None,
                "source_type": "scatter_gather",
                "issue": "OIC parallel activity — Boomi Branch executes sequentially.",
                "resolution": "Implement as Boomi Branch; verify order-independence.",
                "severity": "medium",
            })

        # EBS adapter invokes
        ebs_invokes = [
            inv for inv in self._get_invokes()
            if _adapter_id(inv.get("connection") or {}) in ("EBS", "ORACLE_EBS")
        ]
        for inv in ebs_invokes:
            gaps.append({
                "flow_name": integration_name,
                "step_sequence": None,
                "source_type": "oracle_ebs_api",
                "issue": f"Oracle EBS adapter invoke: {_conn_name(inv.get('connection', {}))} — check for native EBS connector in Boomi account.",
                "resolution": "Run connector discovery before generating. Fallback: DatabaseV2 + PL/SQL.",
                "severity": "high",
            })

        # B2B adapter
        b2b_invokes = [
            inv for inv in self._get_invokes()
            if _adapter_id(inv.get("connection") or {}) in ("B2B", "EDI_MAPPER")
        ]
        for inv in b2b_invokes:
            gaps.append({
                "flow_name": integration_name,
                "step_sequence": None,
                "source_type": "b2b_send",
                "issue": "OIC B2B/EDI adapter — requires Trading Partner component in Boomi.",
                "resolution": "Create Trading Partner + EDI profile in Boomi GUI.",
                "severity": "high",
            })

        return gaps

    # -- error handling ------------------------------------------------------

    def build_error_handling(self) -> dict:
        fault = self.raw.get("faultHandlers") or self.raw.get("faultPolicy") or {}
        has_handler = bool(fault)
        strategies = []
        if has_handler:
            strategies.append({
                "error_type": "ANY",
                "strategy": "propagate",
                "boomi_equivalent": "try_catch_rethrow",
            })
        return {
            "has_error_handler": has_handler,
            "strategies": strategies,
        }

    # -- boomi suggestions ---------------------------------------------------

    def build_boomi_suggestions(self, integration_name: str) -> dict:
        style = self._get_style()
        pattern = OIC_STYLE_MAP.get(style, "orchestration")
        trigger_adapter = self._get_primary_trigger_adapter()
        trigger_type = OIC_TRIGGER_TYPE_MAP.get(trigger_adapter, "http_listener")

        invokes = self._get_invokes()
        connections_needed = list({
            OIC_ADAPTER_MAP.get(_adapter_id(inv.get("connection") or {}), {}).get("boomi", "rest_connection")
            for inv in invokes
        })

        has_ebs = any(
            _adapter_id(inv.get("connection") or {}) in ("EBS", "ORACLE_EBS")
            for inv in invokes
        )
        has_b2b = any(
            _adapter_id(inv.get("connection") or {}) in ("B2B", "EDI_MAPPER")
            for inv in invokes
        )

        complexity = "low"
        if has_ebs or has_b2b or len(invokes) > 3:
            complexity = "high"
        elif self._get_mappings() or len(invokes) > 1:
            complexity = "medium"

        step_components = []
        if trigger_type == "http_listener":
            step_components.append("WSS_Listener")
        elif trigger_type in ("sftp_listener", "file_listener"):
            step_components.append("Disk_V2_Listen")
        elif trigger_type == "scheduler_cron":
            step_components.append("Schedule")
        elif trigger_type == "jms_listener":
            step_components.append("Event_Streams_Listen")

        if self._get_mappings():
            step_components.append("Map_Component")

        for inv in invokes:
            adapter = _adapter_id(inv.get("connection") or {})
            step_type = OIC_INVOKE_STEP_MAP.get(adapter, "http_request")
            step_components.append(BOOMI_STEP_MAP.get(step_type, "REST_Connector"))

        return {
            "process_name": f"MIG_{integration_name}",
            "pattern": pattern,
            "trigger_component": step_components[0] if step_components else "Schedule",
            "step_components": step_components[1:] if len(step_components) > 1 else step_components,
            "connections_needed": connections_needed,
            "complexity": complexity,
            "manual_review_required": has_ebs or has_b2b,
            "notes": (
                "EBS adapter — verify native connector availability in Boomi account. "
                if has_ebs else ""
            ),
        }

    # -- main ----------------------------------------------------------------

    def to_spec_integration(self) -> dict:
        raw_name = self.raw.get("name") or self.raw.get("id") or "integration"
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")

        return {
            "name": safe_name,
            "source_name": raw_name,
            "oic_id": self.raw.get("id", ""),
            "oic_style": self._get_style(),
            "oic_version": self.raw.get("version", ""),
            "oic_status": self.raw.get("status", "UNKNOWN"),
            "flow_type": "primary",
            "trigger": self.build_trigger(),
            "steps": self.build_steps(),
            "error_handling": self.build_error_handling(),
            "boomi_suggestions": self.build_boomi_suggestions(safe_name),
        }


# ---------------------------------------------------------------------------
# Live OIC pull
# ---------------------------------------------------------------------------

def pull_from_oic(host: str, username: str, password: str,
                  port: int = 443, api_version: str = "v3",
                  name_filter: str = None) -> List[dict]:
    print(f"  Connecting to OIC: https://{host}:{port}")
    client = OicApiClient(host, username, password, port, api_version)
    print("  Fetching integration list...")
    listing = client.list_integrations(name_filter)
    print(f"  Found {len(listing)} integrations")

    details = []
    for item in listing:
        int_id = item.get("id", "")
        int_name = item.get("name", int_id)
        try:
            print(f"    Fetching detail: {int_name}")
            detail = client.get_integration(int_id)
            details.append(detail)
        except Exception as e:
            print(f"    WARNING: Could not fetch detail for {int_name}: {e} — using listing record")
            details.append(item)

    return details


# ---------------------------------------------------------------------------
# Local .iar directory scan
# ---------------------------------------------------------------------------

def scan_iar_directory(source_dir: str) -> List[dict]:
    base = Path(source_dir)
    iar_files = list(base.rglob("*.iar")) + list(base.rglob("*.zip"))
    if not iar_files:
        print(f"  WARNING: No .iar or .zip files found in {source_dir}")
        return []

    print(f"  Found {len(iar_files)} archive(s) in {source_dir}")
    results = []
    for f in iar_files:
        print(f"    Parsing: {f.name}")
        parser = IarParser(str(f))
        meta = parser.extract_metadata()
        if meta:
            # Ensure id/name are present
            meta.setdefault("id", f.stem)
            meta.setdefault("name", f.stem)
            results.append(meta)
        else:
            # Create minimal stub so we don't silently drop it
            results.append({
                "id": f.stem,
                "name": f.stem,
                "style": "ORCHESTRATION",
                "status": "UNKNOWN",
                "_source": str(f),
                "_parse_failed": True,
            })

    return results


# ---------------------------------------------------------------------------
# Main spec builder
# ---------------------------------------------------------------------------

def build_spec(integrations: List[dict], project_name: str) -> dict:
    all_connections: Dict[str, dict] = {}
    all_integrations = []
    all_gaps = []

    for raw in integrations:
        if raw.get("_parse_failed"):
            print(f"  SKIPPING (parse failed): {raw.get('name')}")
            all_gaps.append({
                "flow_name": raw.get("name"),
                "step_sequence": None,
                "source_type": "unknown",
                "issue": f"Could not parse .iar archive: {raw.get('_source', '')}",
                "resolution": "Export via OIC UI and inspect manually.",
                "severity": "blocked",
            })
            continue

        mapper = OicIntegrationMapper(raw)
        spec_int = mapper.to_spec_integration()

        connections = mapper.build_connections()
        for k, v in connections.items():
            if k not in all_connections:
                all_connections[k] = v

        all_integrations.append(spec_int)
        all_gaps.extend(mapper.build_gaps(spec_int["name"]))

    notes_parts = []
    if all_gaps:
        high = sum(1 for g in all_gaps if g["severity"] == "high")
        blocked = sum(1 for g in all_gaps if g["severity"] == "blocked")
        if high:
            notes_parts.append(f"{high} high-severity gap(s) require manual decisions.")
        if blocked:
            notes_parts.append(f"{blocked} integration(s) blocked — could not parse .iar.")

    return {
        "schema_version": "1.0",
        "source_system": "oracle_oic",
        "source_version": "OIC Gen3",
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "project_name": project_name,
        "connections": all_connections,
        "integrations": all_integrations,
        "gaps": all_gaps,
        "migration_notes": " ".join(notes_parts),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Oracle Integration Cloud (OIC) → Boomi migration analyzer"
    )
    parser.add_argument("--project",     required=True,  help="Project name for output file naming")
    parser.add_argument("--source-dir",  default=None,   help="Directory of .iar export files (skips live pull)")
    parser.add_argument("--oic-host",    default=None,   help="OIC hostname (overrides ORACLE_OIC_HOST env var)")
    parser.add_argument("--oic-port",    type=int, default=None, help="OIC port (default 443)")
    parser.add_argument("--oic-version", default=None,   help="OIC API version (default v3)")
    parser.add_argument("--filter",      default=None,   help="Filter integrations by name pattern (e.g. 'Customer*')")
    parser.add_argument("--output",      default=None,   help="Output spec path (default: migration-specs/<project>.json)")
    args = parser.parse_args()

    output_path = args.output or os.path.join(
        os.path.dirname(__file__), "..", "migration-specs", f"{args.project}.json"
    )
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    integrations: List[dict] = []

    if args.source_dir:
        print(f"\n[OIC ANALYZER] Scanning local .iar files in: {args.source_dir}")
        integrations = scan_iar_directory(args.source_dir)
    else:
        host = args.oic_host or os.environ.get("ORACLE_OIC_HOST", "")
        username = os.environ.get("ORACLE_OIC_USERNAME", "")
        password = os.environ.get("ORACLE_OIC_PASSWORD", "")
        port = args.oic_port or int(os.environ.get("ORACLE_OIC_PORT", "443"))
        api_version = args.oic_version or os.environ.get("ORACLE_OIC_VERSION", "v3")

        if not host:
            print("ERROR: No OIC host specified. Set ORACLE_OIC_HOST in .env or pass --oic-host.", file=sys.stderr)
            sys.exit(1)
        if not username or not password:
            print("ERROR: ORACLE_OIC_USERNAME and ORACLE_OIC_PASSWORD must be set in .env.", file=sys.stderr)
            sys.exit(1)
        if not _HAS_REQUESTS:
            print("ERROR: requests library required for live pull. Run: pip install requests", file=sys.stderr)
            sys.exit(1)

        print(f"\n[OIC ANALYZER] Live pull from: https://{host}:{port} (API {api_version})")
        try:
            integrations = pull_from_oic(host, username, password, port, api_version, args.filter)
        except Exception as e:
            print(f"ERROR: OIC API call failed: {e}", file=sys.stderr)
            sys.exit(1)

    if not integrations:
        print("WARNING: No integrations found — empty spec will be written.")

    print(f"\n[OIC ANALYZER] Building migration spec for {len(integrations)} integration(s)...")
    spec = build_spec(integrations, args.project)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)

    n_int = len(spec["integrations"])
    n_conn = len(spec["connections"])
    n_gap = len(spec["gaps"])
    print(f"  Spec written: {output_path}")
    print(f"  {n_int} integration(s)  |  {n_conn} connection(s)  |  {n_gap} gap(s)")
    print(f"\n[OIC ANALYZER] Done.")


if __name__ == "__main__":
    main()
