#!/usr/bin/env python3
"""
Oracle SOA Suite (11g/12c) + Oracle EBS → Boomi Migration Analyzer

Pulls BPEL composites from a live Oracle SOA Suite instance via its REST
Management API, parses composite.xml / BPEL / JCA adapter configs, and
produces a normalized migration spec JSON.

Supported artifact sources (checked in priority order):
  1. Live Oracle SOA Suite REST API  (--soa-host / env vars)
  2. Local SAR zip files             (--source-dir with .sar / .zip files)
  3. Expanded composite directories  (--source-dir with composite.xml files)

Usage:
  # Pull from live SOA Suite and analyze all composites
  python analyzers/analyze_oracle_soa.py --project my-project

  # Analyze only composites whose name matches a pattern
  python analyzers/analyze_oracle_soa.py --composite-filter "Order*" --project orders

  # Analyze a specific partition (default: default)
  python analyzers/analyze_oracle_soa.py --partition OrderManagement --project orders

  # Use locally exported SAR files (no live system needed)
  python analyzers/analyze_oracle_soa.py --source-dir /path/to/sars/ --project my-project

  # Write spec to a custom path
  python analyzers/analyze_oracle_soa.py --project my-project --output migration-specs/my-project.json

Output:
  migration-specs/<project>.json
"""

import sys
import os
import re
import json
import zipfile
import tempfile
import base64
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


# ---------------------------------------------------------------------------
# XML namespace registries
# ---------------------------------------------------------------------------

# BPEL namespace detection — both 1.1 and 2.0 are in production use on EBS/SOA
BPEL_NS_11 = "http://schemas.xmlsoap.org/ws/2003/03/business-process/"
BPEL_NS_20 = "http://docs.oasis-open.org/wsbpel/2.0/process/executable"

# Oracle SCA composite namespace
SCA_NS   = "http://xmlns.oracle.com/sca/1.0"
SCA_NS2  = "http://www.osoa.org/xmlns/sca/1.0"

# WS partner-link type namespace (BPEL 1.1)
PLNK_NS  = "http://schemas.xmlsoap.org/ws/2003/05/partner-link/"
# WS partner-link type namespace (BPEL 2.0)
PLNK_NS2 = "http://docs.oasis-open.org/wsbpel/2.0/plnktype"

def _ns(tag: str, ns: str) -> str:
    return f"{{{ns}}}{tag}"

def _strip_ns(tag: str) -> str:
    """Remove XML namespace prefix from a tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


# ---------------------------------------------------------------------------
# JCA adapter class → canonical connection type
# ---------------------------------------------------------------------------
JCA_ADAPTER_MAP: Dict[str, Dict[str, str]] = {
    # Oracle DB Adapter (standard DB operations)
    "oracle.tip.adapter.db.OracleDBAdapter":                {"type": "db",         "driver": "oracle",     "boomi": "databasev2_connection"},
    "oracle.tip.adapter.db.OracleApplicationsAdapterImpl":  {"type": "oracle_ebs", "driver": "ebs",        "boomi": "oracle_ebs_connection"},
    "oracle.tip.adapter.apps.AppsAdapter":                  {"type": "oracle_ebs", "driver": "ebs",        "boomi": "oracle_ebs_connection"},
    # File adapters
    "oracle.tip.adapter.file.FileAdapter":                  {"type": "file",       "driver": "local",      "boomi": "diskv2_connection"},
    "oracle.tip.adapter.ftp.FTPAdapter":                    {"type": "sftp",       "driver": "ftp",        "boomi": "diskv2_connection"},
    # Messaging adapters
    "oracle.tip.adapter.mq.MQSeriesAdapter":                {"type": "jms",        "driver": "mq",         "boomi": "event_streams_connection"},
    "oracle.tip.adapter.jms.IJmsAdapter":                   {"type": "jms",        "driver": "jms",        "boomi": "event_streams_connection"},
    "oracle.tip.adapter.aq.AQAdapter":                      {"type": "oracle_aq",  "driver": "aq",         "boomi": "event_streams_connection"},
    # Web services / HTTP
    "oracle.tip.adapter.http.HttpAdapter":                  {"type": "http_request","driver": "http",      "boomi": "rest_connection"},
    "oracle.tip.adapter.soa.ws.WSAdapter":                  {"type": "http_request","driver": "soap",      "boomi": "rest_connection"},
    "oracle.tip.adapter.ws.WSAdapter":                      {"type": "http_request","driver": "soap",      "boomi": "rest_connection"},
    # Oracle B2B / EDI
    "oracle.tip.adapter.b2b.B2BAdapter":                    {"type": "b2b",        "driver": "b2b",        "boomi": "trading_partner"},
    # LDAP
    "oracle.tip.adapter.ldap.LDAPAdapter":                  {"type": "ldap",       "driver": "ldap",       "boomi": "custom"},
    # Siebel
    "oracle.tip.adapter.siebel.SiebelAdapter":              {"type": "siebel",     "driver": "siebel",     "boomi": "custom"},
    # SAP (sometimes used alongside EBS)
    "oracle.tip.adapter.sap.SAPAdapter":                    {"type": "sap",        "driver": "sap",        "boomi": "boomi_for_sap_connection"},
    # Socket
    "oracle.tip.adapter.socket.SocketAdapter":              {"type": "socket",     "driver": "socket",     "boomi": "custom"},
}

def _resolve_jca_adapter(adapter_class: str) -> Dict[str, str]:
    """Match adapter class to known type; return custom if unknown."""
    for known_class, info in JCA_ADAPTER_MAP.items():
        if known_class.lower() in adapter_class.lower() or adapter_class.lower() in known_class.lower():
            return info
    # Partial match fallback
    cls_lower = adapter_class.lower()
    if "db" in cls_lower or "database" in cls_lower:
        return {"type": "db", "driver": "oracle", "boomi": "databasev2_connection"}
    if "file" in cls_lower:
        return {"type": "file", "driver": "local", "boomi": "diskv2_connection"}
    if "ftp" in cls_lower or "sftp" in cls_lower:
        return {"type": "sftp", "driver": "ftp", "boomi": "diskv2_connection"}
    if "jms" in cls_lower or "mq" in cls_lower or "aq" in cls_lower:
        return {"type": "jms", "driver": "jms", "boomi": "event_streams_connection"}
    if "http" in cls_lower or "ws" in cls_lower or "soap" in cls_lower:
        return {"type": "http_request", "driver": "soap", "boomi": "rest_connection"}
    if "ebs" in cls_lower or "apps" in cls_lower or "application" in cls_lower:
        return {"type": "oracle_ebs", "driver": "ebs", "boomi": "oracle_ebs_connection"}
    return {"type": "custom", "driver": "unknown", "boomi": "custom"}


# ---------------------------------------------------------------------------
# BPEL activity type → canonical step type
# ---------------------------------------------------------------------------
BPEL_ACTIVITY_MAP: Dict[str, str] = {
    "receive":             "bpel_receive",   # refined later based on adapter
    "invoke":              "bpel_invoke",    # refined later based on adapter
    "reply":               "bpel_reply",
    "assign":              "transform",
    "if":                  "choice_router",
    "switch":              "choice_router",   # BPEL 1.1 equivalent of if
    "while":               "foreach",
    "repeatUntil":         "foreach",
    "forEach":             "foreach",
    "flow":                "scatter_gather",  # parallel in BPEL → Branch in Boomi (gap)
    "sequence":            "_inline",         # transparent — embed children
    "scope":               "try_scope",
    "faultHandlers":       "_error_handler",
    "catch":               "_catch",
    "catchAll":            "_catch_all",
    "throw":               "raise_error",
    "rethrow":             "raise_error",
    "exit":                "raise_error",
    "terminate":           "raise_error",
    "wait":                "bpel_wait",
    "pick":                "choice_router",   # event-based pick
    "compensate":          "custom",
    "compensateScope":     "custom",
    "validate":            "custom",
    "extensionActivity":   "custom",
    "empty":               "_noop",
    "opaqueActivity":      "custom",
}

BPEL_INVOKE_TO_STEP: Dict[str, str] = {
    "db":         "db_select",      # refined later by operation type
    "oracle_ebs": "oracle_ebs_api",
    "file":       "file_write",
    "sftp":       "sftp_write",
    "jms":        "jms_publish",
    "oracle_aq":  "jms_publish",
    "http_request": "http_request",
    "b2b":        "custom",
    "sap":        "custom",
    "custom":     "custom",
}

BPEL_RECEIVE_TO_TRIGGER: Dict[str, str] = {
    "db":           "db_event",
    "oracle_ebs":   "oracle_ebs_event",
    "file":         "file_listener",
    "sftp":         "sftp_listener",
    "jms":          "jms_listener",
    "oracle_aq":    "jms_listener",
    "http_request": "http_listener",
    "b2b":          "b2b_listener",
    "custom":       "custom_listener",
}

BOOMI_STEP_SUGGESTIONS: Dict[str, Dict[str, str]] = {
    "db_select":         {"boomi_step": "DatabaseV2_GET",      "complexity": "low"},
    "db_insert":         {"boomi_step": "DatabaseV2_INSERT",   "complexity": "low"},
    "db_update":         {"boomi_step": "DatabaseV2_UPDATE",   "complexity": "low"},
    "db_delete":         {"boomi_step": "DatabaseV2_DELETE",   "complexity": "low"},
    "db_stored_procedure": {"boomi_step": "DatabaseV2_Stored_Proc", "complexity": "medium"},
    "oracle_ebs_api":    {"boomi_step": "REST_Connector_or_Oracle_EBS_Connector", "complexity": "high"},
    "file_listener":     {"boomi_step": "Start_DiskV2_Listen", "complexity": "low"},
    "file_write":        {"boomi_step": "DiskV2_CREATE",       "complexity": "low"},
    "file_read":         {"boomi_step": "DiskV2_GET",          "complexity": "low"},
    "sftp_listener":     {"boomi_step": "Start_DiskV2_Listen_SFTP", "complexity": "low"},
    "sftp_write":        {"boomi_step": "DiskV2_CREATE",       "complexity": "low"},
    "jms_listener":      {"boomi_step": "Start_EventStreams_Listen", "complexity": "medium"},
    "jms_publish":       {"boomi_step": "EventStreams_PRODUCE", "complexity": "medium"},
    "http_listener":     {"boomi_step": "Start_WSS_Listen",    "complexity": "low"},
    "http_request":      {"boomi_step": "REST_Connector",      "complexity": "low"},
    "transform":         {"boomi_step": "Map",                 "complexity": "medium"},
    "choice_router":     {"boomi_step": "Decision",            "complexity": "low"},
    "scatter_gather":    {"boomi_step": "Branch",              "complexity": "medium"},
    "foreach":           {"boomi_step": "Data_Process_Split",  "complexity": "medium"},
    "try_scope":         {"boomi_step": "Try_Catch",           "complexity": "low"},
    "raise_error":       {"boomi_step": "Exception",           "complexity": "low"},
    "bpel_reply":        {"boomi_step": "Return_Documents",    "complexity": "low"},
    "bpel_wait":         {"boomi_step": "REVIEW_REQUIRED",     "complexity": "high"},
    "custom":            {"boomi_step": "REVIEW_REQUIRED",     "complexity": "high"},
}


# ---------------------------------------------------------------------------
# Env-var extraction helper
# ---------------------------------------------------------------------------
def _extract_env_vars(text: str) -> List[str]:
    if not text:
        return []
    patterns = [
        r'\$\{([\w./-]+)\}',           # ${var.name}
        r'@\{([\w./-]+)\}',            # @{var.name}  (Oracle EL)
        r'%%([A-Z_]+)%%',              # %%VAR%%
    ]
    found = set()
    for pat in patterns:
        found.update(re.findall(pat, text or ""))
    return sorted(found)


# ---------------------------------------------------------------------------
# SOA Suite REST API puller
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        return
    with open(env_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


class OracleSoaPuller:
    """
    Pulls composite metadata and SARs from Oracle SOA Suite REST Management API.

    Endpoints (SOA 12c / 11g):
      GET /soa-infra/resources/composites
      GET /soa-infra/resources/composites/{name}/{revision}
      GET /em/webresources/composite/{name}/{revision}/zip   (EM export, optional)
    """

    def __init__(self, host: str, port: str, username: str, password: str,
                 partition: str = "default", em_port: Optional[str] = None):
        self.base_url = f"http://{host}:{port}/soa-infra/resources"
        self.em_url   = f"http://{host}:{em_port or port}/em/webresources" if em_port else None
        self.auth     = (username, password)
        self.partition = partition
        self.session  = requests.Session() if _HAS_REQUESTS else None
        if self.session:
            self.session.auth = self.auth
            self.session.headers.update({"Accept": "application/json"})

    def _get(self, url: str, params: Optional[dict] = None,
             accept: str = "application/json") -> Optional[Any]:
        if not _HAS_REQUESTS:
            print("ERROR: 'requests' library not installed. Run: pip install requests", file=sys.stderr)
            return None
        try:
            self.session.headers["Accept"] = accept
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            if "json" in accept:
                return resp.json()
            return resp.content
        except Exception as exc:
            print(f"  WARNING: GET {url} failed: {exc}", file=sys.stderr)
            return None

    def list_composites(self, name_filter: Optional[str] = None) -> List[Dict]:
        """Return list of composite descriptors from SOA REST API."""
        params: Dict[str, str] = {}
        if self.partition:
            params["partitionName"] = self.partition
        if name_filter:
            params["application"] = name_filter

        data = self._get(f"{self.base_url}/composites", params=params)
        if not data:
            return []

        # SOA 12c response shape: {"compositeInfo": [...]} or {"items": [...]}
        if isinstance(data, dict):
            for key in ("compositeInfo", "items", "composite", "composites"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            # Flat list at top level
            if isinstance(data, list):
                return data
        if isinstance(data, list):
            return data
        return []

    def get_composite_detail(self, name: str, revision: str = "1.0") -> Optional[Dict]:
        """Return full composite descriptor (components, services, references)."""
        url = f"{self.base_url}/composites/{name}/{revision}"
        return self._get(url)

    def download_sar(self, name: str, revision: str, dest_dir: str) -> Optional[str]:
        """
        Try to download the composite SAR zip file.
        Returns path to downloaded file, or None if unavailable.
        """
        # Try EM endpoint first (most reliable for source retrieval)
        if self.em_url:
            url = f"{self.em_url}/composite/{name}/{revision}/zip"
            content = self._get(url, accept="application/zip")
            if content and len(content) > 100:
                out_path = os.path.join(dest_dir, f"{name}_{revision}.zip")
                with open(out_path, "wb") as f:
                    f.write(content)
                print(f"    Downloaded SAR via EM: {name}_{revision}.zip")
                return out_path

        # Try SOA deployer endpoint (11g style)
        url = f"{self.base_url}/composites/{name}/{revision}/sar"
        content = self._get(url, accept="application/zip")
        if content and len(content) > 100:
            out_path = os.path.join(dest_dir, f"{name}_{revision}.sar")
            with open(out_path, "wb") as f:
                f.write(content)
            print(f"    Downloaded SAR: {name}_{revision}.sar")
            return out_path

        return None


# ---------------------------------------------------------------------------
# SAR (composite archive) extractor
# ---------------------------------------------------------------------------

def extract_sar(sar_path: str, dest_dir: str) -> Optional[str]:
    """Extract a SAR/ZIP file. Returns the extraction directory, or None on failure."""
    name = Path(sar_path).stem
    out_dir = os.path.join(dest_dir, name)
    os.makedirs(out_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(sar_path, "r") as zf:
            zf.extractall(out_dir)
        return out_dir
    except zipfile.BadZipFile as exc:
        print(f"  WARNING: Could not extract {sar_path}: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# composite.xml parser
# ---------------------------------------------------------------------------

class CompositeParser:
    """Parses Oracle SCA composite.xml to extract component topology."""

    def __init__(self, composite_path: str):
        self.path = composite_path
        self.root = ET.parse(composite_path).getroot()
        # Detect namespace
        tag = self.root.tag
        if tag.startswith("{"):
            self.sca_ns = tag[1:tag.index("}")]
        else:
            self.sca_ns = SCA_NS

    def _find_all(self, tag: str) -> List[ET.Element]:
        results = self.root.findall(f".//{{{self.sca_ns}}}{tag}")
        if not results:
            results = self.root.findall(f".//{tag}")
        return results

    def get_composite_name(self) -> str:
        return self.root.get("name", Path(self.path).parent.name)

    def get_revision(self) -> str:
        return self.root.get("revision", "1.0")

    def get_components(self) -> List[Dict]:
        """Return list of SCA components: BPEL, Mediator, HumanTask, Decision, etc."""
        components = []
        for comp in self._find_all("component"):
            name = comp.get("name", "")
            # Determine component type from child elements
            comp_type = "unknown"
            implementation_el = None
            for child in comp:
                child_tag = _strip_ns(child.tag)
                if "implementation" in child_tag.lower():
                    implementation_el = child
                    if "bpel" in child_tag.lower():
                        comp_type = "bpel"
                    elif "mediator" in child_tag.lower():
                        comp_type = "mediator"
                    elif "humantask" in child_tag.lower() or "human" in child_tag.lower():
                        comp_type = "humantask"
                    elif "decision" in child_tag.lower() or "rules" in child_tag.lower():
                        comp_type = "decision"
                    elif "spring" in child_tag.lower():
                        comp_type = "spring"
                    elif "bpmn" in child_tag.lower():
                        comp_type = "bpmn"
                    else:
                        comp_type = child_tag.replace("implementation.", "")

            src = ""
            if implementation_el is not None:
                src = implementation_el.get("src", implementation_el.get("process", ""))

            components.append({
                "name": name,
                "type": comp_type,
                "src": src,  # relative path to .bpel / .mplan file
                "properties": _parse_properties(comp),
            })
        return components

    def get_services(self) -> List[Dict]:
        """Inbound services (triggers) of the composite."""
        services = []
        for svc in self._find_all("service"):
            name = svc.get("name", "")
            binding = _detect_binding(svc)
            services.append({
                "name": name,
                "binding": binding,
                "component": svc.get("ui:wires", ""),
            })
        return services

    def get_references(self) -> List[Dict]:
        """Outbound references (external systems the composite calls)."""
        refs = []
        for ref in self._find_all("reference"):
            name = ref.get("name", "")
            binding = _detect_binding(ref)
            refs.append({
                "name": name,
                "binding": binding,
            })
        return refs


def _detect_binding(el: ET.Element) -> Dict:
    """Extract binding info (JCA, WS, HTTP, etc.) from a service/reference element."""
    for child in el:
        tag = _strip_ns(child.tag)
        if "binding" in tag.lower():
            binding_type = tag.replace("binding.", "").replace("Binding", "").lower()
            result = {"type": binding_type}
            # JCA binding has a "config" attribute pointing to the .jca file
            if "jca" in binding_type:
                result["config"] = child.get("config", "")
            elif "ws" in binding_type or "wsdl" in binding_type:
                result["wsdl"] = child.get("wsdl", child.get("port", ""))
            elif "http" in binding_type:
                result["uri"] = child.get("uri", "")
            return result
    return {"type": "unknown"}


def _parse_properties(el: ET.Element) -> Dict:
    """Extract <property> children into a dict."""
    props = {}
    for child in el:
        if "property" in _strip_ns(child.tag).lower():
            key = child.get("name", child.get("key", ""))
            val = child.text or child.get("value", "")
            if key:
                props[key] = val
    return props


# ---------------------------------------------------------------------------
# JCA adapter config parser
# ---------------------------------------------------------------------------

class JcaParser:
    """Parses Oracle JCA adapter binding descriptor (.jca files)."""

    def __init__(self, jca_path: str):
        self.path = jca_path
        try:
            self.root = ET.parse(jca_path).getroot()
        except ET.ParseError:
            self.root = None

    def get_adapter_info(self) -> Dict:
        if self.root is None:
            return {"type": "custom", "driver": "unknown", "boomi": "custom"}

        # Oracle JCA schema: <adapter-config ... adapter="...">
        adapter_class = self.root.get("adapter", "")
        connection_factory = self.root.get("connection-factory", "")
        location = ""

        # Look for connection-factory or activation-spec element
        for child in self.root.iter():
            tag = _strip_ns(child.tag)
            if "connection-factory" in tag.lower():
                location = child.get("location", "")
                adapter_class = adapter_class or child.get("UIConnectionName", "")
            elif "activation-spec" in tag.lower():
                # Try to detect from ClassName attribute
                cls = child.get("ClassName", "")
                if cls and not adapter_class:
                    adapter_class = cls
            elif "interaction-spec" in tag.lower():
                cls = child.get("ClassName", "")
                if cls and not adapter_class:
                    adapter_class = cls

        adapter_type = _resolve_jca_adapter(adapter_class or connection_factory)

        # Extract connection parameters
        conn_params = self._extract_connection_params()

        return {
            **adapter_type,
            "adapter_class": adapter_class,
            "connection_factory_location": location,
            "conn_params": conn_params,
        }

    def _extract_connection_params(self) -> Dict:
        """Extract JNDI location, schema, directory, etc. from the JCA file."""
        params = {}
        if self.root is None:
            return params
        for child in self.root.iter():
            tag = _strip_ns(child.tag)
            for attr_name in ("location", "jndiName", "SchemaName", "DirectoryPath",
                              "FileNamingConvention", "QueueName", "TopicName",
                              "DestinationName", "Endpoint", "TargetNamespace"):
                val = child.get(attr_name, "")
                if val:
                    params[attr_name] = val
        return params


# ---------------------------------------------------------------------------
# BPEL process parser
# ---------------------------------------------------------------------------

class BpelParser:
    """
    Parses a BPEL 1.1 or 2.0 process file into a normalized activity tree.
    Supports both namespaces transparently.
    """

    def __init__(self, bpel_path: str, partner_link_types: Optional[Dict] = None):
        self.path = bpel_path
        self.partner_link_types = partner_link_types or {}  # partnerLinkName → adapter info
        try:
            self.tree = ET.parse(bpel_path)
            self.root = self.tree.getroot()
        except ET.ParseError as exc:
            print(f"  WARNING: Could not parse BPEL {bpel_path}: {exc}", file=sys.stderr)
            self.root = None

        # Auto-detect namespace
        if self.root is not None:
            tag = self.root.tag
            if BPEL_NS_20 in tag:
                self.ns = BPEL_NS_20
            elif BPEL_NS_11 in tag:
                self.ns = BPEL_NS_11
            else:
                # Some Oracle BPEL files omit namespace on root — scan children
                self.ns = self._sniff_namespace()
        else:
            self.ns = BPEL_NS_20

    def _sniff_namespace(self) -> str:
        if self.root is None:
            return BPEL_NS_20
        for child in self.root.iter():
            if BPEL_NS_20 in child.tag:
                return BPEL_NS_20
            if BPEL_NS_11 in child.tag:
                return BPEL_NS_11
        return BPEL_NS_20

    def _bns(self, tag: str) -> str:
        return f"{{{self.ns}}}{tag}"

    def get_process_name(self) -> str:
        if self.root is None:
            return Path(self.path).stem
        return self.root.get("name", Path(self.path).stem)

    def get_partner_links(self) -> Dict[str, Dict]:
        """Extract partnerLinks declared in the process."""
        if self.root is None:
            return {}
        pl_map = {}
        for pl_container in [self.root.find(self._bns("partnerLinks")),
                              self.root.find("partnerLinks")]:
            if pl_container is None:
                continue
            for pl in pl_container:
                name = pl.get("name", "")
                pl_type = pl.get("partnerLinkType", "")
                my_role = pl.get("myRole", "")
                partner_role = pl.get("partnerRole", "")
                # Case-insensitive adapter lookup — JCA stems are lowercased, BPEL names are not
                adapter_info = (
                    self.partner_link_types.get(name)
                    or self.partner_link_types.get(name.lower())
                    or self.partner_link_types.get(name.replace(" ", "_").lower())
                    or {}
                )
                pl_map[name] = {
                    "partnerLinkType": pl_type,
                    "myRole": my_role,
                    "partnerRole": partner_role,
                    "adapter_info": adapter_info,
                }
        return pl_map

    def get_variables(self) -> List[Dict]:
        """Extract variable declarations."""
        if self.root is None:
            return []
        vars_out = []
        container = self.root.find(self._bns("variables")) or self.root.find("variables")
        if container is None:
            return []
        for var in container:
            vars_out.append({
                "name": var.get("name", ""),
                "message_type": var.get("messageType", ""),
                "element": var.get("element", ""),
                "type": var.get("type", ""),
            })
        return vars_out

    def extract_steps(self, sequence_start: int = 1,
                      flow_name: str = "") -> Tuple[List[Dict], List[Dict]]:
        """
        Walk the BPEL process and return (steps, gaps).
        Entry point is the root <process> element's child sequence.
        """
        if self.root is None:
            return [], []

        partner_links = self.get_partner_links()
        gaps = []
        steps = []
        seq = [sequence_start]

        # Find the top-level sequence or flow
        for tag in ["sequence", "flow", "scope"]:
            el = self.root.find(self._bns(tag)) or self.root.find(tag)
            if el is not None:
                self._walk(el, steps, gaps, partner_links, seq, flow_name)
                break
        else:
            # Walk all direct children
            for child in self.root:
                tag = _strip_ns(child.tag)
                if tag.lower() not in ("partnerlinks", "variables", "correlationsets",
                                       "faulthandlers", "eventhandlers", "documentation",
                                       "import", "extensions"):
                    self._walk(child, steps, gaps, partner_links, seq, flow_name)

        return steps, gaps

    def _walk(self, el: ET.Element, steps: List[Dict], gaps: List[Dict],
               partner_links: Dict, seq: List[int],
               flow_name: str = "") -> None:
        """Recursively walk a BPEL element and populate steps."""
        raw_tag = _strip_ns(el.tag)
        # Normalize: remove namespace junk, lowercase for lookup
        tag_lower = raw_tag.lower()

        canonical_type = BPEL_ACTIVITY_MAP.get(raw_tag) or BPEL_ACTIVITY_MAP.get(tag_lower)

        if canonical_type is None:
            # Try prefix match
            for bpel_tag, ctype in BPEL_ACTIVITY_MAP.items():
                if tag_lower.endswith(bpel_tag.lower()):
                    canonical_type = ctype
                    break

        if canonical_type is None:
            return  # Unknown element, skip silently

        # Transparent containers — walk children without emitting a step
        if canonical_type in ("_inline", "_noop"):
            for child in el:
                self._walk(child, steps, gaps, partner_links, seq, flow_name)
            return

        if canonical_type == "_error_handler":
            # faultHandlers — walk catch/catchAll
            for child in el:
                self._walk(child, steps, gaps, partner_links, seq, flow_name)
            return

        step_name = el.get("name", raw_tag)
        partner_link = el.get("partnerLink", "")
        operation = el.get("operation", "")
        variable = el.get("variable", "")
        current_seq = seq[0]
        seq[0] += 1

        step: Dict[str, Any] = {
            "sequence":      current_seq,
            "type":          canonical_type,
            "label":         step_name,
            "partner_link":  partner_link,
            "operation":     operation,
            "variable":      variable,
            "requires_review": False,
            "_source_ref":   f"{flow_name}:step-{current_seq}",
        }

        # --------------- Refine invoke/receive based on adapter ---------------
        if canonical_type in ("bpel_invoke", "bpel_receive"):
            pl_info = partner_links.get(partner_link, {})
            adapter_info = pl_info.get("adapter_info", {})
            conn_type = adapter_info.get("type", "http_request")  # default: web service call

            if canonical_type == "bpel_invoke":
                refined = BPEL_INVOKE_TO_STEP.get(conn_type, "http_request")
                # Determine DB operation from operation name heuristic
                if conn_type == "db":
                    op_lower = operation.lower()
                    if "insert" in op_lower or "create" in op_lower:
                        refined = "db_insert"
                    elif "update" in op_lower or "merge" in op_lower:
                        refined = "db_update"
                    elif "delete" in op_lower or "remove" in op_lower:
                        refined = "db_delete"
                    elif "exec" in op_lower or "proc" in op_lower or "sp_" in op_lower:
                        refined = "db_stored_procedure"
                    else:
                        refined = "db_select"
                step["type"] = refined
                step["adapter_type"] = conn_type
                step["config_ref"] = partner_link
                step["boomi_step"] = BOOMI_STEP_SUGGESTIONS.get(refined, {}).get("boomi_step", "REVIEW_REQUIRED")
                step["complexity"] = BOOMI_STEP_SUGGESTIONS.get(refined, {}).get("complexity", "medium")

            elif canonical_type == "bpel_receive":
                # First receive with createInstance → trigger
                if el.get("createInstance", "no").lower() == "yes":
                    step["type"] = "trigger"
                    trigger_type = BPEL_RECEIVE_TO_TRIGGER.get(conn_type, "http_listener")
                    step["trigger_type"] = trigger_type
                    step["adapter_type"] = conn_type
                    step["config_ref"] = partner_link
                else:
                    step["type"] = "http_request"  # receive mid-flow = correlation
                    step["adapter_type"] = conn_type

        # --------------- assign → transform with field mappings ---------------
        elif canonical_type == "transform":
            copies = self._extract_assign_copies(el)
            step["field_mappings"] = copies
            step["boomi_step"] = "Map or Set_Properties"
            step["complexity"] = "low" if len(copies) <= 5 else "medium"
            # Simple assigns with only 1-2 copies → Set Properties
            if len(copies) <= 2 and all(
                c.get("from_literal") or c.get("from_expression", "").count(".") <= 1
                for c in copies
            ):
                step["boomi_step"] = "Set_Properties"

        # --------------- scope → try/catch (capture fault handlers) ----------
        elif canonical_type == "try_scope":
            fault_el = el.find(self._bns("faultHandlers")) or el.find("faultHandlers")
            if fault_el is not None:
                step["has_fault_handler"] = True
                step["fault_types"] = [
                    c.get("faultName", "*")
                    for c in (fault_el.findall(self._bns("catch"))
                              + fault_el.findall("catch"))
                ]
            step["boomi_step"] = "Try_Catch"
            step["complexity"] = "low"
            # Walk inner sequence
            inner_seq = el.find(self._bns("sequence")) or el.find("sequence")
            if inner_seq is not None:
                child_steps = []
                for child in inner_seq:
                    self._walk(child, child_steps, gaps, partner_links, seq, flow_name)
                step["substeps"] = child_steps

        # --------------- flow → Branch (note parallelism gap) ----------------
        elif canonical_type == "scatter_gather":
            gap = {
                "flow_name":           flow_name,
                "step_sequence":       current_seq,
                "source_type":         "bpel_flow",
                "issue":               "BPEL <flow> executes branches in parallel. Boomi Branch step is sequential.",
                "resolution":          "Implemented as Boomi Branch (sequential). Parallel execution not preserved.",
                "severity":            "medium",
                "behavioral_difference": "Parallel → sequential execution",
                "suggested_resolution":  "Use separate Boomi processes + Event Streams if true parallelism is needed",
                "decision_required":     True,
                "auto_resolved":         False,
            }
            gaps.append(gap)
            step["boomi_step"] = "Branch"
            step["complexity"] = "medium"
            step["requires_review"] = True
            step["gap_note"] = gap["issue"]
            child_steps = []
            for child in el:
                self._walk(child, child_steps, gaps, partner_links, seq, flow_name)
            step["substeps"] = child_steps

        # --------------- choice_router / if / switch -------------------------
        elif canonical_type == "choice_router":
            branches = self._extract_conditions(el)
            step["branches"] = branches
            step["boomi_step"] = "Decision" if len(branches) <= 2 else "Route"
            step["complexity"] = "low"

        # --------------- foreach / while / repeatUntil ----------------------
        elif canonical_type == "foreach":
            step["boomi_step"] = "Data_Process_Split"
            step["complexity"] = "medium"
            count_expr = el.get("finalCounterValue", el.get("counterName", ""))
            step["loop_variable"] = count_expr
            child_steps = []
            inner = el.find(self._bns("scope")) or el.find("scope") or el
            for child in inner:
                self._walk(child, child_steps, gaps, partner_links, seq, flow_name)
            step["substeps"] = child_steps

        # --------------- bpel_wait — no direct Boomi equivalent -------------
        elif canonical_type == "bpel_wait":
            dur = el.find(self._bns("for")) or el.find("for")
            until = el.find(self._bns("until")) or el.find("until")
            step["wait_duration"] = dur.text if dur is not None else ""
            step["wait_until"] = until.text if until is not None else ""
            step["boomi_step"] = "REVIEW_REQUIRED"
            step["complexity"] = "high"
            step["requires_review"] = True
            gaps.append({
                "flow_name":        flow_name,
                "step_sequence":    current_seq,
                "source_type":      "bpel_wait",
                "issue":            f"BPEL <wait> (timer) has no direct Boomi equivalent.",
                "resolution":       "Consider using a scheduled sub-process or external timer mechanism.",
                "severity":         "high",
                "behavioral_difference": "Timer-based suspension not natively supported in Boomi integration flows",
                "suggested_resolution":  "Split into two processes; use a scheduler or message store to bridge the wait",
                "decision_required":     True,
                "auto_resolved":         False,
            })

        # --------------- catch/catchAll → error branch ----------------------
        elif canonical_type in ("_catch", "_catch_all"):
            fault_name = el.get("faultName", "*")
            step["fault_name"] = fault_name
            step["type"] = "error_handler"
            step["boomi_step"] = "Catch_Branch"
            step["complexity"] = "low"
            child_steps = []
            for child in el:
                self._walk(child, child_steps, gaps, partner_links, seq, flow_name)
            step["substeps"] = child_steps

        # --------------- custom / extensionActivity -------------------------
        elif canonical_type == "custom":
            step["requires_review"] = True
            step["boomi_step"] = "REVIEW_REQUIRED"
            step["complexity"] = "high"
            step["raw_xml_tag"] = raw_tag
            gaps.append({
                "flow_name":        flow_name,
                "step_sequence":    current_seq,
                "source_type":      raw_tag,
                "issue":            f"BPEL extension activity '{raw_tag}' has no automatic mapping.",
                "resolution":       "Requires manual implementation review.",
                "severity":         "high",
                "behavioral_difference": "Oracle-specific extension, no standard equivalent",
                "suggested_resolution":  "Implement in Groovy Data Process step or split into sub-process",
                "decision_required":     True,
                "auto_resolved":         False,
            })

        # Apply boomi suggestions from lookup table if not already set
        if "boomi_step" not in step:
            sug = BOOMI_STEP_SUGGESTIONS.get(step["type"], {})
            step["boomi_step"] = sug.get("boomi_step", "REVIEW_REQUIRED")
            step["complexity"] = sug.get("complexity", "medium")

        # Mark as requires_review if complexity is high
        if step.get("complexity") == "high":
            step["requires_review"] = True

        steps.append(step)

    def _extract_assign_copies(self, assign_el: ET.Element) -> List[Dict]:
        """Parse <assign><copy><from>...</from><to>...</to></copy></assign>."""
        copies = []
        for copy in assign_el.findall(self._bns("copy")) + assign_el.findall("copy"):
            from_el = copy.find(self._bns("from")) or copy.find("from")
            to_el = copy.find(self._bns("to")) or copy.find("to")
            if from_el is None or to_el is None:
                continue
            copies.append({
                "source":         from_el.get("variable", from_el.get("part", "")),
                "source_path":    from_el.get("query", from_el.get("property", "")),
                "from_literal":   from_el.text.strip() if from_el.text else "",
                "from_expression": from_el.get("expression", ""),
                "target":         to_el.get("variable", to_el.get("part", "")),
                "target_path":    to_el.get("query", to_el.get("property", "")),
                "type":           "string",
                "transformation": "passthrough",
            })
        return copies

    def _extract_conditions(self, if_el: ET.Element) -> List[Dict]:
        """Extract branches from <if>/<elseif>/<else> or <switch>/<case>."""
        branches = []
        # BPEL 2.0 if/elseif/else
        cond_el = if_el.find(self._bns("condition")) or if_el.find("condition")
        if cond_el is not None:
            branches.append({
                "condition": cond_el.text or "",
                "branch_type": "if",
            })
        for elseif in (if_el.findall(self._bns("elseif")) + if_el.findall("elseif")):
            c = elseif.find(self._bns("condition")) or elseif.find("condition")
            branches.append({
                "condition": c.text if c is not None else "",
                "branch_type": "elseif",
            })
        else_el = if_el.find(self._bns("else")) or if_el.find("else")
        if else_el is not None:
            branches.append({"condition": "otherwise", "branch_type": "else"})

        # BPEL 1.1 switch/case
        if not branches:
            for case in (if_el.findall(self._bns("case")) + if_el.findall("case")):
                cond = case.get("condition", "")
                branches.append({"condition": cond, "branch_type": "case"})
            otherwise = if_el.find(self._bns("otherwise")) or if_el.find("otherwise")
            if otherwise is not None:
                branches.append({"condition": "otherwise", "branch_type": "otherwise"})

        return branches


# ---------------------------------------------------------------------------
# Oracle SOA REST API response → partial IR (when SAR not available)
# ---------------------------------------------------------------------------

def _rest_composite_to_ir(composite_detail: Dict, composite_name: str) -> Dict:
    """
    Build a best-effort canonical flow from Oracle SOA REST API JSON response
    (used when we cannot download the SAR/BPEL source).
    """
    components = composite_detail.get("components", composite_detail.get("component", []))
    services   = composite_detail.get("services", composite_detail.get("service", []))
    references = composite_detail.get("references", composite_detail.get("reference", []))

    steps = []
    connections = {}

    for i, ref in enumerate(references if isinstance(references, list) else []):
        ref_name = ref.get("name", f"Reference_{i}")
        steps.append({
            "sequence": i + 1,
            "type": "http_request",
            "label": ref_name,
            "config_ref": ref_name,
            "boomi_step": "REST_Connector",
            "complexity": "medium",
            "requires_review": True,
            "_source_ref": f"{composite_name}:step-{i+1}",
            "note": "Derived from SOA REST API — no BPEL source available. Review binding details.",
        })
        connections[ref_name] = {
            "type": "http_request",
            "boomi_equivalent": "rest_connection",
            "notes": f"Reference '{ref_name}' from composite. Configure binding in Boomi.",
        }

    trigger_type = "http_listener"
    for svc in (services if isinstance(services, list) else []):
        binding = svc.get("binding", {})
        if isinstance(binding, dict):
            bt = binding.get("type", "").lower()
            if "jms" in bt or "aq" in bt:
                trigger_type = "jms_listener"
            elif "file" in bt or "ftp" in bt:
                trigger_type = "file_listener"
            break

    return {
        "steps": steps,
        "connections": connections,
        "trigger_type": trigger_type,
        "source": "soa_rest_api_metadata",
    }


# ---------------------------------------------------------------------------
# Full composite analyzer
# ---------------------------------------------------------------------------

def analyze_composite_dir(composite_dir: str, composite_name: str,
                           rest_detail: Optional[Dict] = None) -> Dict:
    """
    Analyze an extracted composite directory (contains composite.xml, .bpel, .jca files).
    Returns a dict with flows, connections, and gaps.
    """
    flows = []
    connections = {}
    all_gaps = []

    composite_xml = os.path.join(composite_dir, "composite.xml")
    if not os.path.isfile(composite_xml):
        # Search one level deeper
        for dirpath, dirnames, filenames in os.walk(composite_dir):
            for fn in filenames:
                if fn == "composite.xml":
                    composite_xml = os.path.join(dirpath, fn)
                    break
            if os.path.isfile(composite_xml):
                break

    if not os.path.isfile(composite_xml):
        print(f"  WARNING: No composite.xml found in {composite_dir}", file=sys.stderr)
        if rest_detail:
            ir = _rest_composite_to_ir(rest_detail, composite_name)
            return {"flows": [], "connections": ir["connections"], "gaps": []}
        return {"flows": [], "connections": {}, "gaps": []}

    comp_parser = CompositeParser(composite_xml)
    sca_name   = comp_parser.get_composite_name() or composite_name
    revision   = comp_parser.get_revision()
    components = comp_parser.get_components()
    services   = comp_parser.get_services()
    references = comp_parser.get_references()

    composite_base = os.path.dirname(composite_xml)

    # Build partner-link → adapter mapping from JCA files.
    # Only populate pl_adapter_map here — connections are registered below from composite.xml
    # so they get proper camel-cased names without duplicates.
    _ADAPTER_SUFFIXES = ("_receive", "_produce", "_invoke", "_aq", "_jms",
                         "_ftp", "_sftp", "_db", "_http", "_ws", "_b2b")
    pl_adapter_map: Dict[str, Dict] = {}
    jca_files = list(Path(composite_base).rglob("*.jca"))
    for jca_path in jca_files:
        jca_p = JcaParser(str(jca_path))
        adapter_info = jca_p.get_adapter_info()
        # Index by full stem (lowercase) and also by stripped stem (adapter suffix removed)
        full_stem = jca_path.stem.lower()
        stripped = full_stem
        for suffix in _ADAPTER_SUFFIXES:
            if stripped.endswith(suffix):
                stripped = stripped[: -len(suffix)]
                break
        pl_adapter_map[full_stem] = adapter_info
        if stripped != full_stem:
            pl_adapter_map[stripped] = adapter_info

    # Build service connections from composite services
    for svc in services:
        binding = svc.get("binding", {})
        btype   = binding.get("type", "unknown")
        svc_name = svc.get("name", "Service")
        if "jca" in btype:
            jca_file = binding.get("config", "")
            if jca_file:
                jca_full = os.path.join(composite_base, jca_file.lstrip("/"))
                if os.path.isfile(jca_full):
                    jca_p = JcaParser(jca_full)
                    adapter_info = jca_p.get_adapter_info()
                    pl_adapter_map[svc_name.lower()] = adapter_info
                    connections[svc_name] = _adapter_to_connection(adapter_info, svc_name)

    # Build reference connections from composite references
    for ref in references:
        binding  = ref.get("binding", {})
        btype    = binding.get("type", "unknown")
        ref_name = ref.get("name", "Reference")
        if "jca" in btype:
            jca_file = binding.get("config", "")
            if jca_file:
                jca_full = os.path.join(composite_base, jca_file.lstrip("/"))
                if os.path.isfile(jca_full):
                    jca_p = JcaParser(jca_full)
                    adapter_info = jca_p.get_adapter_info()
                    pl_adapter_map[ref_name.lower()] = adapter_info
                    connections[ref_name] = _adapter_to_connection(adapter_info, ref_name)
        elif "ws" in btype or "http" in btype:
            connections[ref_name] = {
                "type": "http_request",
                "wsdl": binding.get("wsdl", ""),
                "boomi_equivalent": "rest_connection",
                "notes": f"Web service reference '{ref_name}'. Migrate to REST connector.",
            }

    # Parse each BPEL component
    for comp in components:
        if comp["type"] != "bpel":
            # Non-BPEL components (Mediator, HumanTask, etc.) → flag as gap
            if comp["type"] in ("mediator", "humantask", "decision", "bpmn"):
                all_gaps.append({
                    "flow_name":     comp["name"],
                    "step_sequence": 0,
                    "source_type":   f"oracle_sca_{comp['type']}",
                    "issue":         f"Oracle SCA component type '{comp['type']}' is not BPEL — requires manual analysis.",
                    "resolution":    f"Review {comp['type']} logic and implement equivalent in Boomi.",
                    "severity":      "high",
                    "behavioral_difference": f"{comp['type']} has no direct Boomi equivalent",
                    "suggested_resolution":  _non_bpel_suggestion(comp["type"]),
                    "decision_required": True,
                    "auto_resolved":     False,
                })
            continue

        bpel_file = comp.get("src", "")
        if not bpel_file:
            bpel_file = comp["name"] + ".bpel"

        # Resolve relative path
        bpel_full = os.path.join(composite_base, bpel_file)
        if not os.path.isfile(bpel_full):
            # Search recursively
            found = list(Path(composite_base).rglob(f"*{Path(bpel_file).stem}*.bpel"))
            bpel_full = str(found[0]) if found else ""

        if not os.path.isfile(bpel_full):
            print(f"  WARNING: BPEL file not found for component '{comp['name']}'", file=sys.stderr)
            continue

        print(f"  Parsing BPEL: {os.path.basename(bpel_full)}")
        bpel_p = BpelParser(bpel_full, partner_link_types=pl_adapter_map)

        steps, gaps = bpel_p.extract_steps(flow_name=comp["name"])
        all_gaps.extend(gaps)

        # Determine trigger from steps (first trigger-typed step)
        trigger = None
        for s in steps:
            if s.get("type") == "trigger":
                trigger = {
                    "type": s.get("trigger_type", "http_listener"),
                    "config_ref": s.get("config_ref", ""),
                    "adapter_type": s.get("adapter_type", ""),
                    "partner_link": s.get("partner_link", ""),
                }
                steps.remove(s)
                break

        # If no trigger found, default to manual/scheduled
        if trigger is None:
            trigger = {"type": "scheduler_fixed", "config_ref": "", "notes": "Trigger not detected — defaulted to scheduled. Review."}

        # Determine pattern
        pattern = _infer_pattern(trigger, steps)
        complexity = _overall_complexity(steps)
        manual_review = any(s.get("requires_review") for s in steps)

        flow = {
            "name":        _safe_name(comp["name"]),
            "source_name": comp["name"],
            "flow_type":   "primary",
            "trigger":     trigger,
            "steps":       steps,
            "variables":   [v["name"] for v in bpel_p.get_variables()],
            "error_handling": {
                "has_error_handler": any(g["source_type"] == "_catch" for g in gaps if g.get("flow_name") == comp["name"]),
                "strategies": [],
            },
            "boomi_suggestions": {
                "process_name":          f"MIG_SOA_{_safe_name(comp['name'])}_Process",
                "pattern":               pattern,
                "trigger_component":     _trigger_component(trigger),
                "step_components":       [s.get("boomi_step", "REVIEW") for s in steps[:8]],
                "connections_needed":    list({s.get("config_ref", "") for s in steps if s.get("config_ref")} - {""}),
                "complexity":            complexity,
                "manual_review_required": manual_review,
                "notes":                 f"Migrated from Oracle SOA BPEL process '{comp['name']}' in composite '{sca_name}'",
            },
        }
        flows.append(flow)

    return {"flows": flows, "connections": connections, "gaps": all_gaps}


def _non_bpel_suggestion(comp_type: str) -> str:
    mapping = {
        "mediator":  "Implement routing logic as Boomi Decision/Route shapes with Set Properties for content-based routing",
        "humantask": "Use Boomi Flow for human task management, or implement approval via email + webhook callback",
        "decision":  "Implement business rules as Boomi Decision shapes or Groovy scripting in Data Process",
        "bpmn":      "Analyze BPMN flow and map each task to equivalent Boomi shapes",
        "spring":    "Spring bean components require manual re-implementation as Boomi Groovy scripts",
    }
    return mapping.get(comp_type, f"Manually analyze {comp_type} component and implement equivalent Boomi logic")


def _adapter_to_connection(adapter_info: Dict, ref_name: str) -> Dict:
    conn_type  = adapter_info.get("type", "custom")
    driver     = adapter_info.get("driver", "")
    boomi_eq   = adapter_info.get("boomi", "custom")
    conn_params = adapter_info.get("conn_params", {})
    env_vars   = _extract_env_vars(json.dumps(conn_params))

    conn: Dict[str, Any] = {
        "type":           conn_type,
        "boomi_equivalent": boomi_eq,
        "adapter_class":  adapter_info.get("adapter_class", ""),
        "notes":          f"JCA adapter '{ref_name}'. Configure credentials in Boomi environment extensions.",
    }
    if env_vars:
        conn["env_vars"] = env_vars
    if conn_params:
        conn.update({k: v for k, v in conn_params.items() if v})
    if driver:
        conn["driver"] = driver
    return conn


# ---------------------------------------------------------------------------
# Pattern / complexity inference helpers
# ---------------------------------------------------------------------------

def _infer_pattern(trigger: Dict, steps: List[Dict]) -> str:
    ttype = trigger.get("type", "")
    step_types = {s.get("type", "") for s in steps}

    if "http" in ttype or "wss" in ttype:
        return "request_reply_api"
    if "scheduler" in ttype or "cron" in ttype:
        step_types_lower = {t.lower() for t in step_types}
        if any("db" in t for t in step_types_lower) and any("upsert" in t or "insert" in t for t in step_types_lower):
            return "system_sync"
        return "scheduled_batch"
    if "file" in ttype or "sftp" in ttype:
        return "file_processing"
    if "jms" in ttype or "mq" in ttype or "aq" in ttype:
        return "event_driven"
    if "b2b" in ttype:
        return "b2b_document_exchange"
    return "custom"


def _overall_complexity(steps: List[Dict]) -> str:
    if any(s.get("complexity") == "high" or s.get("requires_review") for s in steps):
        return "high"
    if any(s.get("complexity") == "medium" for s in steps):
        return "medium"
    return "low"


def _trigger_component(trigger: Dict) -> str:
    ttype = trigger.get("type", "")
    mapping = {
        "http_listener":    "WSS_Listener",
        "jms_listener":     "EventStreams_Listen",
        "oracle_aq":        "EventStreams_Listen",
        "file_listener":    "DiskV2_Listen",
        "sftp_listener":    "DiskV2_Listen_SFTP",
        "scheduler_fixed":  "Start_Schedule",
        "scheduler_cron":   "Start_Schedule_Cron",
        "oracle_ebs_event": "REVIEW_REQUIRED",
        "b2b_listener":     "TradingPartner_Start",
    }
    return mapping.get(ttype, "Start_Manual")


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w-]", "_", name).strip("_")


# ---------------------------------------------------------------------------
# Top-level spec builder
# ---------------------------------------------------------------------------

def build_spec(project_name: str, source_system: str,
               flows: List[Dict], connections: Dict, gaps: List[Dict],
               source_version: str = "12c") -> Dict:
    return {
        "schema_version": "1.0",
        "source_system":  source_system,
        "source_version": source_version,
        "analyzed_at":    datetime.now(timezone.utc).isoformat(),
        "project_name":   project_name,
        "connections":    connections,
        "integrations":   flows,
        "gaps":           gaps,
        "migration_notes": (
            f"Generated by analyze_oracle_soa.py from {source_system} composites. "
            f"Review all steps marked requires_review=true before generating Boomi processes. "
            f"Oracle EBS adapters may require the Oracle EBS native connector in Boomi — "
            f"run boomi-component-search.sh to check if it is available in the account."
        ),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Oracle SOA Suite / EBS → Boomi migration analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Source options
    src_group = parser.add_mutually_exclusive_group()
    src_group.add_argument("--source-dir",
                           help="Local directory of SAR zips or expanded composite directories")
    src_group.add_argument("--soa-host",
                           help="Oracle SOA host (overrides ORACLE_SOA_HOST env var)")

    # SOA connection settings
    parser.add_argument("--soa-port",     default=None, help="SOA port (default: ORACLE_SOA_PORT or 7001)")
    parser.add_argument("--em-port",      default=None, help="EM Console port for SAR export (optional)")
    parser.add_argument("--soa-username", default=None, help="SOA username (default: ORACLE_SOA_USERNAME)")
    parser.add_argument("--partition",    default=None, help="SOA partition (default: ORACLE_SOA_PARTITION or 'default')")
    parser.add_argument("--composite-filter", default=None, help="Composite name filter (supports wildcards, e.g. 'Order*')")

    # Output settings
    parser.add_argument("--project",      default=None, help="Project name for output spec")
    parser.add_argument("--output",       default=None, help="Output spec file path")
    parser.add_argument("--source-version", default="12c", help="Oracle SOA version (11g/12c, default: 12c)")

    args = parser.parse_args()

    # Resolve credentials from args or env
    soa_host     = args.soa_host     or os.environ.get("ORACLE_SOA_HOST", "")
    soa_port     = args.soa_port     or os.environ.get("ORACLE_SOA_PORT", "7001")
    em_port      = args.em_port      or os.environ.get("ORACLE_SOA_EM_PORT", "")
    soa_username = args.soa_username or os.environ.get("ORACLE_SOA_USERNAME", "weblogic")
    soa_password = os.environ.get("ORACLE_SOA_PASSWORD", "")
    partition    = args.partition    or os.environ.get("ORACLE_SOA_PARTITION", "default")

    project_name = args.project or "oracle_soa_migration"
    project_name = project_name.lower().replace(" ", "_").replace("-", "_")

    output_dir  = Path(__file__).parent.parent / "migration-specs"
    output_dir.mkdir(exist_ok=True)
    output_path = Path(args.output) if args.output else (output_dir / f"{project_name}.json")

    all_flows: List[Dict]  = []
    all_connections: Dict  = {}
    all_gaps: List[Dict]   = []

    with tempfile.TemporaryDirectory() as tmpdir:

        # ── Path A: Local SAR / composite directory ────────────────────────
        if args.source_dir:
            source_path = Path(args.source_dir)
            if not source_path.exists():
                print(f"ERROR: --source-dir '{args.source_dir}' not found.", file=sys.stderr)
                sys.exit(1)

            print(f"[ANALYZE] Scanning local source directory: {source_path}")

            # Collect SAR/ZIP files
            sar_files = list(source_path.rglob("*.sar")) + list(source_path.rglob("*.zip"))
            composite_dirs = []

            for sar in sar_files:
                print(f"  Extracting: {sar.name}")
                extracted = extract_sar(str(sar), tmpdir)
                if extracted:
                    composite_dirs.append((extracted, sar.stem))

            # Also check for already-expanded directories (have composite.xml)
            for dirpath in source_path.rglob("composite.xml"):
                comp_dir = str(dirpath.parent)
                comp_name = dirpath.parent.name
                composite_dirs.append((comp_dir, comp_name))

            if not composite_dirs:
                print("ERROR: No SAR files or composite.xml directories found in --source-dir", file=sys.stderr)
                sys.exit(1)

            for comp_dir, comp_name in composite_dirs:
                if args.composite_filter:
                    import fnmatch
                    if not fnmatch.fnmatch(comp_name, args.composite_filter):
                        continue
                print(f"\n  Analyzing composite: {comp_name}")
                result = analyze_composite_dir(comp_dir, comp_name)
                all_flows.extend(result["flows"])
                all_connections.update(result["connections"])
                all_gaps.extend(result["gaps"])

        # ── Path B: Live Oracle SOA Suite REST API ─────────────────────────
        else:
            if not soa_host:
                print("ERROR: No source provided. Use --source-dir or set ORACLE_SOA_HOST in .env", file=sys.stderr)
                sys.exit(1)
            if not _HAS_REQUESTS:
                print("ERROR: 'requests' library required for live pull. Run: pip install requests", file=sys.stderr)
                sys.exit(1)
            if not soa_password:
                print("ERROR: ORACLE_SOA_PASSWORD not set in .env or environment.", file=sys.stderr)
                sys.exit(1)

            print(f"[PULL] Connecting to Oracle SOA Suite at {soa_host}:{soa_port} (partition: {partition})")

            puller = OracleSoaPuller(
                host=soa_host, port=soa_port, username=soa_username,
                password=soa_password, partition=partition, em_port=em_port or None,
            )

            composites = puller.list_composites(name_filter=args.composite_filter)
            if not composites:
                print("WARNING: No composites returned from SOA REST API. Check credentials and partition.", file=sys.stderr)
                sys.exit(1)

            print(f"  Found {len(composites)} composite(s)")

            for comp in composites:
                comp_name = comp.get("compositeName", comp.get("name", ""))
                revision  = comp.get("revision", comp.get("compositeRevision", "1.0"))
                if not comp_name:
                    continue

                print(f"\n  Processing: {comp_name} (rev {revision})")

                # Try to download SAR
                sar_path = puller.download_sar(comp_name, revision, tmpdir)
                if sar_path:
                    ext_dir = extract_sar(sar_path, tmpdir)
                    if ext_dir:
                        detail = puller.get_composite_detail(comp_name, revision)
                        result = analyze_composite_dir(ext_dir, comp_name, rest_detail=detail)
                        all_flows.extend(result["flows"])
                        all_connections.update(result["connections"])
                        all_gaps.extend(result["gaps"])
                        continue

                # SAR not available — use REST API metadata only
                print(f"    SAR not available — using SOA REST API metadata only")
                detail = puller.get_composite_detail(comp_name, revision)
                if detail:
                    ir = _rest_composite_to_ir(detail, comp_name)
                    all_connections.update(ir["connections"])
                    trigger = {"type": ir["trigger_type"], "config_ref": ""}
                    all_flows.append({
                        "name":        _safe_name(comp_name),
                        "source_name": comp_name,
                        "flow_type":   "primary",
                        "trigger":     trigger,
                        "steps":       ir["steps"],
                        "error_handling": {"has_error_handler": False, "strategies": []},
                        "boomi_suggestions": {
                            "process_name":          f"MIG_SOA_{_safe_name(comp_name)}_Process",
                            "pattern":               _infer_pattern(trigger, ir["steps"]),
                            "trigger_component":     _trigger_component(trigger),
                            "step_components":       [],
                            "connections_needed":    list(ir["connections"].keys()),
                            "complexity":            "high",
                            "manual_review_required": True,
                            "notes":                 "Derived from SOA REST API metadata only — BPEL source not available",
                        },
                    })

    # ── Build spec ─────────────────────────────────────────────────────────
    if not all_flows and not all_connections:
        print("WARNING: No flows or connections extracted. Check source data.", file=sys.stderr)

    spec = build_spec(
        project_name=project_name,
        source_system="oracle_soa",
        flows=all_flows,
        connections=all_connections,
        gaps=all_gaps,
        source_version=args.source_version,
    )

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, default=str)

    print(f"\n[DONE] Spec written to: {output_path}")
    print(f"  Flows:       {len(all_flows)}")
    print(f"  Connections: {len(all_connections)}")
    print(f"  Gaps:        {len(all_gaps)}")
    print(f"\nNext steps:")
    print(f"  1. Review gaps in the spec (requires_review=true steps need attention)")
    print(f"  2. Check for Oracle EBS connector in Boomi: boomi-component-search.sh --name '%EBS%' --type connector-settings")
    print(f"  3. Run enrichment: python enrichers/enrich_spec.py {output_path}")
    print(f"  4. Generate Boomi processes: python generators/generate_boomi.py {output_path} --project MIG_{project_name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
