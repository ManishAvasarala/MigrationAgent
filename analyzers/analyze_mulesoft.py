#!/usr/bin/env python3
"""
MuleSoft 4.x -> Boomi Migration Analyzer

Parses MuleSoft XML flow files and generates a normalized migration spec JSON.
Supports single XML files or project directories (scans src/main/mule/*.xml).

Usage:
  python analyzers/analyze_mulesoft.py <path-to-xml-or-project-dir>
  python analyzers/analyze_mulesoft.py <path> --output migration-specs/myproject.json
  python analyzers/analyze_mulesoft.py <path> --project-name MyProject

Output: migration-specs/<project-name>.json
"""

import sys
import os
import re
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Namespace registry — maps MuleSoft XML namespace URIs to short prefixes
# ---------------------------------------------------------------------------
NS = {
    'http://www.mulesoft.org/schema/mule/core': 'core',
    'http://www.mulesoft.org/schema/mule/http': 'http',
    'http://www.mulesoft.org/schema/mule/db': 'db',
    'http://www.mulesoft.org/schema/mule/ee/core': 'ee',
    'http://www.mulesoft.org/schema/mule/file': 'file',
    'http://www.mulesoft.org/schema/mule/sftp': 'sftp',
    'http://www.mulesoft.org/schema/mule/ftp': 'ftp',
    'http://www.mulesoft.org/schema/mule/salesforce': 'salesforce',
    'http://www.mulesoft.org/schema/mule/email': 'email',
    'http://www.mulesoft.org/schema/mule/jms': 'jms',
    'http://www.mulesoft.org/schema/mule/vm': 'vm',
    'http://www.mulesoft.org/schema/mule/scripting': 'scripting',
    'http://www.mulesoft.org/schema/mule/batch': 'batch',
    'http://www.mulesoft.org/schema/mule/kafka': 'kafka',
    'http://www.mulesoft.org/schema/mule/amqp': 'amqp',
    'http://www.mulesoft.org/schema/mule/sqs': 'sqs',
    'http://www.mulesoft.org/schema/mule/slack': 'slack',
    'http://www.mulesoft.org/schema/mule/objectstore': 'os',
}

# ---------------------------------------------------------------------------
# Boomi suggestion mappings — keyed by normalized step type
# ---------------------------------------------------------------------------
BOOMI_STEP_MAP = {
    'http_listener':          {'boomi_step': 'Start_WSS_Listen',          'component': 'wss_operation',                    'complexity': 'low'},
    'scheduler_cron':         {'boomi_step': 'Start_Schedule_Cron',        'component': 'start_step',                       'complexity': 'low'},
    'scheduler_fixed':        {'boomi_step': 'Start_Schedule_Fixed',       'component': 'start_step',                       'complexity': 'low'},
    'file_listener':          {'boomi_step': 'Start_DiskV2_Listen',        'component': 'diskv2_connection + diskv2_operation', 'complexity': 'low'},
    'sftp_listener':          {'boomi_step': 'Start_DiskV2_Listen_SFTP',   'component': 'diskv2_connection + diskv2_operation', 'complexity': 'low'},
    'jms_listener':           {'boomi_step': 'Start_EventStreams_Listen',   'component': 'event_streams_connection + listen_operation', 'complexity': 'medium'},
    'vm_listener':            {'boomi_step': 'Start_EventStreams_Listen',   'component': 'event_streams_connection + listen_operation', 'complexity': 'medium'},
    'set_variable':           {'boomi_step': 'Set_Properties',             'component': 'set_properties_step',              'complexity': 'low'},
    'set_payload':            {'boomi_step': 'Message',                    'component': 'message_step',                     'complexity': 'low'},
    'transform':              {'boomi_step': 'Map',                        'component': 'transform.map + profiles',         'complexity': 'medium'},
    'logger':                 {'boomi_step': 'Notify',                     'component': 'notify_step',                      'complexity': 'low'},
    'flow_ref':               {'boomi_step': 'Process_Call',               'component': 'process_call_step',                'complexity': 'low'},
    'choice_router':          {'boomi_step': 'Decision',                   'component': 'decision_step',                    'complexity': 'low'},
    'choice_router_multi':    {'boomi_step': 'Route',                      'component': 'route_step',                       'complexity': 'low'},
    'scatter_gather':         {'boomi_step': 'Branch',                     'component': 'branch_step',                      'complexity': 'medium'},
    'foreach':                {'boomi_step': 'Data_Process_Split',         'component': 'data_process_step',                'complexity': 'medium'},
    'try_scope':              {'boomi_step': 'Try_Catch',                  'component': 'try_catch_step',                   'complexity': 'low'},
    'async_scope':            {'boomi_step': 'REVIEW_REQUIRED',            'component': 'separate_process',                 'complexity': 'high'},
    'batch_job':              {'boomi_step': 'Data_Process_Split',         'component': 'data_process_step + subprocess',   'complexity': 'high'},
    'http_request':           {'boomi_step': 'REST_Connector',             'component': 'rest_connection + rest_operation', 'complexity': 'low'},
    'db_select':              {'boomi_step': 'DatabaseV2_GET',             'component': 'databasev2_connection + databasev2_operation', 'complexity': 'low'},
    'db_insert':              {'boomi_step': 'DatabaseV2_INSERT',          'component': 'databasev2_connection + databasev2_operation', 'complexity': 'low'},
    'db_update':              {'boomi_step': 'DatabaseV2_UPDATE',          'component': 'databasev2_connection + databasev2_operation', 'complexity': 'low'},
    'db_delete':              {'boomi_step': 'DatabaseV2_DELETE',          'component': 'databasev2_connection + databasev2_operation', 'complexity': 'low'},
    'db_stored_procedure':    {'boomi_step': 'DatabaseV2_Stored_Proc',     'component': 'databasev2_connection + databasev2_operation', 'complexity': 'medium'},
    'file_read':              {'boomi_step': 'DiskV2_GET',                 'component': 'diskv2_connection + diskv2_operation', 'complexity': 'low'},
    'file_write':             {'boomi_step': 'DiskV2_CREATE',              'component': 'diskv2_connection + diskv2_operation', 'complexity': 'low'},
    'file_list':              {'boomi_step': 'DiskV2_QUERY',               'component': 'diskv2_connection + diskv2_operation', 'complexity': 'low'},
    'file_delete':            {'boomi_step': 'DiskV2_DELETE',              'component': 'diskv2_connection + diskv2_operation', 'complexity': 'low'},
    'sftp_read':              {'boomi_step': 'DiskV2_GET_SFTP',            'component': 'diskv2_connection(sftp) + diskv2_operation', 'complexity': 'low'},
    'sftp_write':             {'boomi_step': 'DiskV2_CREATE_SFTP',         'component': 'diskv2_connection(sftp) + diskv2_operation', 'complexity': 'low'},
    'salesforce_query':       {'boomi_step': 'Salesforce_Query',           'component': 'salesforce_connection + salesforce_operation', 'complexity': 'low'},
    'salesforce_create':      {'boomi_step': 'Salesforce_Create',          'component': 'salesforce_connection + salesforce_operation', 'complexity': 'low'},
    'salesforce_update':      {'boomi_step': 'Salesforce_Update',          'component': 'salesforce_connection + salesforce_operation', 'complexity': 'low'},
    'salesforce_upsert':      {'boomi_step': 'Salesforce_Upsert',          'component': 'salesforce_connection + salesforce_operation', 'complexity': 'low'},
    'email_send':             {'boomi_step': 'Mail_Connector',             'component': 'http_rest_to_mail_or_mail_connector', 'complexity': 'low'},
    'jms_publish':            {'boomi_step': 'EventStreams_Produce',        'component': 'event_streams_connection + produce_operation', 'complexity': 'medium'},
    'vm_publish':             {'boomi_step': 'EventStreams_Produce',        'component': 'event_streams_connection + produce_operation', 'complexity': 'medium'},
    'raise_error':            {'boomi_step': 'Exception',                  'component': 'exception_step',                   'complexity': 'low'},
    'object_store':           {'boomi_step': 'Document_Cache',             'component': 'document_cache_component',         'complexity': 'medium'},
    'custom':                 {'boomi_step': 'REVIEW_REQUIRED',            'component': 'TBD',                              'complexity': 'high'},
}

BOOMI_CONN_MAP = {
    'http_listener':  'wss_listener',
    'http_request':   'rest_connection',
    'db':             'databasev2_connection',
    'file':           'diskv2_connection',
    'sftp':           'diskv2_connection_sftp',
    'ftp':            'diskv2_connection_ftp',
    'salesforce':     'salesforce_connection',
    'email_smtp':     'mail_connector',
    'jms':            'event_streams_connection',
    'vm':             'event_streams_connection',
    'kafka':          'custom_connector_review',
    'amqp':           'custom_connector_review',
    'sqs':            'rest_connection_aws_sqs',
}


# ─── DataWeave parser ─────────────────────────────────────────────────────────

_DW_COMPLEX_PATTERNS = [
    r'\bif\b', r'\belse\b', r'\bwhen\b', r'\bmatch\b', r'\bcase\b',
    r'\bfilter\b', r'\bflatMap\b', r'\bgroupBy\b', r'\bjoinBy\b',
    r'\bpluck\b', r'\breduce\b', r'\bzip\b', r'\bflatten\b',
    r'\bsizeOf\b', r'\bmax\b', r'\bmin\b', r'\bsum\b',
]

def parse_dataweave_mappings(dw_str):
    """
    Parse a DataWeave 2.0 script into structured field_mappings.
    Returns (field_mappings: list, has_complex_logic: bool, raw_body: str).
    Simple direct mappings (target: source.path) are extracted deterministically.
    Anything using conditionals, functions, or filters is flagged for LLM enrichment.
    """
    if not dw_str:
        return [], True, ""

    # Strip DW header, isolate body after ---
    parts = dw_str.split("---", 1)
    body = parts[1].strip() if len(parts) > 1 else dw_str.strip()

    # Detect complex logic patterns — flag for LLM if present
    has_complex = any(re.search(p, body) for p in _DW_COMPLEX_PATTERNS)

    mappings = []
    # Match simple: fieldName: source.field.path (no parens, no brackets after)
    simple_re = re.compile(
        r'^\s{2,}(\w+):\s+([\w.]+(?:\s+as\s+\w+)?)\s*[,}]?$',
        re.MULTILINE
    )
    for m in simple_re.finditer(body):
        target = m.group(1)
        source_expr = m.group(2).strip()

        # Skip DW keywords and output declarations
        if target in ('output', 'payload', 'vars', 'attributes', 'error',
                      'message', 'items', 'item', 'true', 'false', 'null'):
            continue

        type_cast = "string"
        source_path = source_expr
        if ' as ' in source_expr:
            source_path, cast_raw = source_expr.split(' as ', 1)
            cast_raw = cast_raw.strip().lower()
            type_cast = {
                'string': 'string', 'number': 'decimal', 'integer': 'integer',
                'boolean': 'boolean', 'datetime': 'datetime', 'date': 'datetime',
                'localtime': 'datetime', 'localdatetime': 'datetime',
            }.get(cast_raw, cast_raw)

        mappings.append({
            "source": source_path.strip(),
            "target": target,
            "type": type_cast,
            "transformation": "passthrough",
        })

    return mappings, has_complex, body


def extract_env_vars(text):
    """Extract all ${variable} references from a string."""
    return sorted(set(re.findall(r'\$\{([\w.]+)\}', text or '')))


def tag_ns(tag: str) -> str:
    """Return the short namespace prefix for a Clark-notation tag, e.g. '{ns}local' -> 'http'."""
    if tag.startswith('{'):
        uri, local = tag[1:].split('}', 1)
        return NS.get(uri, 'unknown'), local
    return 'core', tag


def _parse_children(el, skip_local=('error-handler',), seq_start=0):
    """
    Recursively parse direct children of `el` as steps.
    Returns (steps_list, final_seq).
    """
    steps = []
    seq = seq_start
    for child in el:
        _, local = tag_ns(child.tag)
        if local in skip_local:
            continue
        step = classify_step(child)
        seq += 1
        step['sequence'] = seq
        steps.append(step)
    return steps, seq


def attr(el: ET.Element, *names: str, default=None):
    """Return first matching attribute value from an element."""
    for name in names:
        v = el.get(name) or el.get(f'doc:{name}')
        if v:
            return v
    return default


# ---------------------------------------------------------------------------
# Global config parsers
# ---------------------------------------------------------------------------

def parse_http_listener_config(el: ET.Element, name: str) -> dict:
    conn = el.find('.//{http://www.mulesoft.org/schema/mule/http}listener-connection')
    return {
        'type': 'http_listener',
        'host': attr(conn, 'host', default='0.0.0.0') if conn is not None else '0.0.0.0',
        'port': attr(conn, 'port', default='8081') if conn is not None else '8081',
        'boomi_equivalent': BOOMI_CONN_MAP['http_listener'],
        'notes': 'Run boomi-shared-server-info.sh to determine API tier before building',
    }


def parse_http_request_config(el: ET.Element, name: str) -> dict:
    conn_ns = '{http://www.mulesoft.org/schema/mule/http}request-connection'
    conn = el.find(f'.//{conn_ns}')
    auth_type = 'none'
    auth_notes = ''
    if conn is not None:
        basic = conn.find('{http://www.mulesoft.org/schema/mule/http}authentication/{http://www.mulesoft.org/schema/mule/http}basic-authentication')
        bearer = conn.find('{http://www.mulesoft.org/schema/mule/http}authentication/{http://www.mulesoft.org/schema/mule/http}bearer-token-authentication')
        oauth = conn.find('{http://www.mulesoft.org/schema/mule/http}authentication/{http://www.mulesoft.org/schema/mule/http}oauth-client-credentials')
        if basic is not None:
            auth_type = 'basic'
        elif bearer is not None:
            auth_type = 'bearer'
        elif oauth is not None:
            auth_type = 'oauth_client_credentials'
            auth_notes = 'OAuth — may require GUI setup for token endpoint'
    return {
        'type': 'http_request',
        'protocol': attr(conn, 'protocol', default='HTTP') if conn is not None else 'HTTP',
        'host': attr(conn, 'host', default='') if conn is not None else '',
        'port': attr(conn, 'port', default='80') if conn is not None else '80',
        'base_path': attr(conn, 'basePath', default='') if conn is not None else '',
        'auth_type': auth_type,
        'boomi_equivalent': BOOMI_CONN_MAP['http_request'],
        'notes': auth_notes or f'Auth: {auth_type}',
    }


def parse_db_config(el: ET.Element, name: str) -> dict:
    ns_db = 'http://www.mulesoft.org/schema/mule/db'
    driver = 'generic'
    host = ''
    port = ''
    database = ''
    url = ''
    for child in el.iter():
        local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if 'my-sql-connection' in local or 'mysql' in local.lower():
            driver = 'mysql'
            host = attr(child, 'host', default='')
            port = attr(child, 'port', default='3306')
            database = attr(child, 'database', default='')
        elif 'oracle-connection' in local:
            driver = 'oracle'
            host = attr(child, 'host', default='')
            port = attr(child, 'port', default='1521')
            database = attr(child, 'instance', default='')
        elif 'ms-sql-connection' in local or 'sqlserver' in local.lower():
            driver = 'sqlserver'
            host = attr(child, 'host', default='')
            port = attr(child, 'port', default='1433')
            database = attr(child, 'databaseName', default='')
        elif 'generic-connection' in local or 'derby-connection' in local:
            url_val = attr(child, 'url', default='')
            url = url_val
            if 'postgresql' in url_val.lower():
                driver = 'postgresql'
            elif 'mysql' in url_val.lower():
                driver = 'mysql'
            elif 'oracle' in url_val.lower():
                driver = 'oracle'
            elif 'sqlserver' in url_val.lower() or 'mssql' in url_val.lower():
                driver = 'sqlserver'
    all_text = f"{host} {url} {database}"
    return {
        'type': 'db',
        'driver': driver,
        'host': host,
        'port': port,
        'database': database,
        'jdbc_url': url,
        'uses_env_vars': '${' in all_text,
        'env_vars': extract_env_vars(all_text),
        'boomi_equivalent': BOOMI_CONN_MAP['db'],
        'boomi_driver': driver.capitalize(),
        'notes': f'Driver: {driver}',
    }


def parse_sftp_config(el: ET.Element, name: str) -> dict:
    conn = el.find('.//{http://www.mulesoft.org/schema/mule/sftp}connection')
    return {
        'type': 'sftp',
        'host': attr(conn, 'host', default='') if conn is not None else '',
        'port': attr(conn, 'port', default='22') if conn is not None else '22',
        'username': attr(conn, 'username', default='') if conn is not None else '',
        'uses_env_vars': True,
        'boomi_equivalent': BOOMI_CONN_MAP['sftp'],
        'notes': 'Map to Disk V2 connection with SFTP transport',
    }


def parse_file_config(el: ET.Element, name: str) -> dict:
    conn = el.find('.//{http://www.mulesoft.org/schema/mule/file}connection')
    return {
        'type': 'file',
        'working_dir': attr(conn, 'workingDir', default='/') if conn is not None else '/',
        'boomi_equivalent': BOOMI_CONN_MAP['file'],
        'notes': 'Map to Disk V2 connection (local file system)',
    }


def parse_salesforce_config(el: ET.Element, name: str) -> dict:
    return {
        'type': 'salesforce',
        'boomi_equivalent': BOOMI_CONN_MAP['salesforce'],
        'requires_gui_setup': True,
        'notes': 'Pull existing Salesforce connection from Boomi platform first. OAuth setup requires GUI.',
    }


def parse_email_config(el: ET.Element, name: str) -> dict:
    smtp_ns = '{http://www.mulesoft.org/schema/mule/email}smtp-connection'
    conn = el.find(f'.//{smtp_ns}')
    return {
        'type': 'email_smtp',
        'host': attr(conn, 'host', default='') if conn is not None else '',
        'port': attr(conn, 'port', default='587') if conn is not None else '587',
        'boomi_equivalent': BOOMI_CONN_MAP['email_smtp'],
        'notes': 'Use Boomi Mail connector or HTTP connector to mail relay service',
    }


def parse_global_config(el: ET.Element) -> Optional[dict]:
    ns_prefix, local = tag_ns(el.tag)
    name = attr(el, 'name', default='unnamed')
    if ns_prefix == 'http' and local == 'listener-config':
        return name, parse_http_listener_config(el, name)
    if ns_prefix == 'http' and local == 'request-config':
        return name, parse_http_request_config(el, name)
    if ns_prefix == 'db' and local == 'config':
        return name, parse_db_config(el, name)
    if ns_prefix == 'sftp' and local == 'config':
        return name, parse_sftp_config(el, name)
    if ns_prefix == 'file' and local == 'config':
        return name, parse_file_config(el, name)
    if ns_prefix == 'salesforce' and 'config' in local.lower():
        return name, parse_salesforce_config(el, name)
    if ns_prefix == 'email' and 'config' in local.lower():
        return name, parse_email_config(el, name)
    return None


# ---------------------------------------------------------------------------
# Step parsers
# ---------------------------------------------------------------------------

def classify_step(el: ET.Element) -> dict:
    ns_prefix, local = tag_ns(el.tag)
    step = {
        'source_tag': f'{ns_prefix}:{local}',
        'type': 'custom',
        'label': attr(el, 'doc:name', 'name', default=f'{ns_prefix}:{local}'),
        'config_ref': attr(el, 'config-ref', default=None),
        'requires_review': False,
        'boomi_step': 'REVIEW_REQUIRED',
        'boomi_component': 'TBD',
        'complexity': 'high',
    }

    # --- core namespace ---
    if ns_prefix == 'core' or ns_prefix == 'unknown':
        if local == 'set-variable':
            step.update({'type': 'set_variable', 'variable_name': attr(el, 'variableName'), 'value': attr(el, 'value')})
        elif local == 'set-payload':
            step.update({'type': 'set_payload', 'value': attr(el, 'value')})
        elif local == 'logger':
            step.update({'type': 'logger', 'level': attr(el, 'level', default='INFO'), 'message': attr(el, 'message')})
        elif local == 'flow-ref':
            step.update({'type': 'flow_ref', 'target_flow': attr(el, 'name')})
        elif local == 'choice':
            CORE = 'http://www.mulesoft.org/schema/mule/core'
            whens = el.findall(f'{{{CORE}}}when')
            count = len(whens)
            # Extract condition and nested steps from first when-branch
            condition = ""
            true_steps = []
            false_steps = []
            if whens:
                condition = attr(whens[0], 'expression', default='')
                true_steps, _ = _parse_children(whens[0])
            otherwise = el.find(f'{{{CORE}}}otherwise')
            if otherwise is not None:
                false_steps, _ = _parse_children(otherwise)
            # Additional branches beyond the first
            additional = []
            for when_el in whens[1:]:
                b_steps, _ = _parse_children(when_el)
                additional.append({'condition': attr(when_el, 'expression', default=''), 'steps': b_steps})
            step.update({
                'type': 'choice_router_multi' if count > 1 else 'choice_router',
                'branch_count': count + 1,
                'condition': condition,
                'true_steps': true_steps,
                'false_steps': false_steps,
                'additional_branches': additional,
            })
        elif local == 'scatter-gather':
            routes = list(el)
            branch_steps_list = []
            for route_el in routes:
                b_steps, _ = _parse_children(route_el)
                branch_steps_list.append(b_steps)
            step.update({'type': 'scatter_gather', 'route_count': len(routes), 'branches': branch_steps_list})
            step['requires_review'] = True
            step['gap_note'] = 'scatter-gather is parallel in MuleSoft; Boomi Branch is sequential'
        elif local == 'foreach':
            CORE = 'http://www.mulesoft.org/schema/mule/core'
            loop_steps, _ = _parse_children(el, skip_local=('error-handler',))
            step.update({
                'type': 'foreach',
                'collection': attr(el, 'collection'),
                'batch_size': attr(el, 'batchSize'),
                'loop_steps': loop_steps,
            })
        elif local == 'try':
            CORE = 'http://www.mulesoft.org/schema/mule/core'
            monitored, _ = _parse_children(el, skip_local=('error-handler',))
            # Parse error handler strategies
            err_handler = el.find(f'{{{CORE}}}error-handler')
            error_strategies = []
            if err_handler is not None:
                for strategy in err_handler:
                    _, s_local = tag_ns(strategy.tag)
                    err_type = attr(strategy, 'type', default='ANY')
                    handler_steps, _ = _parse_children(strategy)
                    error_strategies.append({
                        'error_type': err_type,
                        'strategy': 'propagate' if 'propagate' in s_local else 'continue',
                        'handler_steps': handler_steps,
                    })
            step.update({
                'type': 'try_scope',
                'monitored_steps': monitored,
                'error_strategies': error_strategies,
            })
        elif local == 'async':
            step.update({'type': 'async_scope'})
            step['requires_review'] = True
            step['gap_note'] = 'async scope has no direct Boomi equivalent; implement as separate process'
        elif local == 'raise-error':
            step.update({'type': 'raise_error', 'error_type': attr(el, 'type'), 'description': attr(el, 'description')})

    # --- http namespace ---
    elif ns_prefix == 'http':
        if local == 'listener':
            path = attr(el, 'path', default='/')
            methods = attr(el, 'allowedMethods', default='GET')
            step.update({'type': 'http_listener', 'path': path, 'allowed_methods': methods})
        elif local == 'request':
            step.update({'type': 'http_request', 'method': attr(el, 'method', default='GET'), 'path': attr(el, 'path', default='/')})

    # --- db namespace ---
    elif ns_prefix == 'db':
        sql_el = el.find('{http://www.mulesoft.org/schema/mule/db}sql')
        sql = sql_el.text.strip() if sql_el is not None and sql_el.text else attr(el, 'sql', default='')
        has_params = el.find('{http://www.mulesoft.org/schema/mule/db}input-parameters') is not None
        if local == 'select':
            step.update({'type': 'db_select', 'sql': sql, 'has_parameters': has_params})
        elif local == 'insert':
            step.update({'type': 'db_insert', 'sql': sql, 'has_parameters': has_params, 'auto_generate_keys': attr(el, 'autoGenerateKeys', default='false') == 'true'})
        elif local == 'update':
            step.update({'type': 'db_update', 'sql': sql, 'has_parameters': has_params})
        elif local == 'delete':
            step.update({'type': 'db_delete', 'sql': sql, 'has_parameters': has_params})
        elif local in ('stored-procedure', 'execute-script'):
            step.update({'type': 'db_stored_procedure', 'sql': sql})

    # --- ee (Enterprise Edition / DataWeave) namespace ---
    elif ns_prefix == 'ee':
        if local == 'transform':
            payload_el = el.find('.//{http://www.mulesoft.org/schema/mule/ee/core}set-payload')
            has_dw = payload_el is not None and payload_el.text and '%dw' in payload_el.text
            vars_els = el.findall('.//{http://www.mulesoft.org/schema/mule/ee/core}set-variable')
            output_format = ''
            raw_script = ''
            field_mappings = []
            has_complex = False

            if payload_el is not None and payload_el.text:
                raw_script = payload_el.text.strip()
                for line in raw_script.split('\n'):
                    if 'output' in line:
                        output_format = line.strip().replace('output', '').strip()
                        break
                # Deterministic field-mapping extraction
                field_mappings, has_complex, _ = parse_dataweave_mappings(raw_script)

            step.update({
                'type': 'transform',
                'has_dataweave': has_dw,
                'output_format': output_format,
                'sets_variables': len(vars_els) > 0,
                'raw_script': raw_script,
                'field_mappings': field_mappings,
                'has_complex_logic': has_complex,
                # Only flag requires_review if mappings are complex or missing
                'requires_review': has_complex or not field_mappings,
            })

    # --- file namespace ---
    elif ns_prefix == 'file':
        if local in ('read', 'copy'):
            step.update({'type': 'file_read', 'path': attr(el, 'path')})
        elif local == 'write':
            step.update({'type': 'file_write', 'path': attr(el, 'path')})
        elif local in ('list', 'matcher'):
            step.update({'type': 'file_list', 'directory': attr(el, 'directoryPath', 'directory')})
        elif local == 'delete':
            step.update({'type': 'file_delete', 'path': attr(el, 'path')})
        elif local == 'listener':
            step.update({'type': 'file_listener', 'directory': attr(el, 'directory', default='/')})

    # --- sftp namespace ---
    elif ns_prefix == 'sftp':
        if local in ('read', 'copy'):
            step.update({'type': 'sftp_read', 'path': attr(el, 'path')})
        elif local == 'write':
            step.update({'type': 'sftp_write', 'path': attr(el, 'path')})
        elif local == 'list':
            step.update({'type': 'sftp_list', 'directory': attr(el, 'directoryPath', 'directory')})
        elif local == 'delete':
            step.update({'type': 'sftp_delete', 'path': attr(el, 'path')})
        elif local == 'listener':
            dir_path = attr(el, 'directory', default='/')
            matcher = el.find('{http://www.mulesoft.org/schema/mule/sftp}matcher')
            post_proc = el.find('{http://www.mulesoft.org/schema/mule/sftp}post-processing-action')
            step.update({
                'type': 'sftp_listener',
                'directory': dir_path,
                'file_pattern': attr(matcher, 'filenamePattern', default='*') if matcher is not None else '*',
                'post_processing': attr(post_proc, 'moveToDirectory', default='delete') if post_proc is not None else 'none',
            })

    # --- salesforce namespace ---
    elif ns_prefix == 'salesforce':
        soql_el = el.find('{http://www.mulesoft.org/schema/mule/salesforce}salesforce-query')
        soql = soql_el.text.strip() if soql_el is not None and soql_el.text else ''
        if local == 'query':
            step.update({'type': 'salesforce_query', 'soql': soql})
        elif local in ('create', 'create-bulk'):
            step.update({'type': 'salesforce_create', 'object_type': attr(el, 'type', default='')})
        elif local in ('update', 'update-bulk'):
            step.update({'type': 'salesforce_update', 'object_type': attr(el, 'type', default='')})
        elif local in ('upsert', 'upsert-bulk'):
            step.update({'type': 'salesforce_upsert', 'object_type': attr(el, 'type', default=''), 'external_id_field': attr(el, 'externalIdFieldName', default='Id')})
        elif local in ('delete', 'delete-bulk'):
            step.update({'type': 'salesforce_delete'})

    # --- email namespace ---
    elif ns_prefix == 'email':
        if local == 'send':
            step.update({'type': 'email_send'})

    # --- jms / vm namespace ---
    elif ns_prefix in ('jms', 'vm'):
        if local in ('publish', 'send'):
            step.update({'type': f'{ns_prefix}_publish', 'destination': attr(el, 'destination')})
        elif local in ('consume', 'listener'):
            step.update({'type': f'{ns_prefix}_listener', 'destination': attr(el, 'destination')})

    # --- batch namespace ---
    elif ns_prefix == 'batch':
        if local == 'job':
            step.update({'type': 'batch_job'})
            step['requires_review'] = True
            step['gap_note'] = 'MuleSoft batch job has no direct Boomi equivalent; use Data Process split + subprocess'

    # Apply Boomi mapping lookup
    step_type = step.get('type', 'custom')
    mapping = BOOMI_STEP_MAP.get(step_type, BOOMI_STEP_MAP['custom'])
    step['boomi_step'] = mapping['boomi_step']
    step['boomi_component'] = mapping['component']
    step['complexity'] = mapping['complexity']
    if mapping['boomi_step'] == 'REVIEW_REQUIRED':
        step['requires_review'] = True

    return step


def parse_flow(el: ET.Element, flow_type: str = 'primary') -> dict:
    name = attr(el, 'name', default='unnamed-flow')
    children = [c for c in el if not c.tag.endswith('}error-handler') and not c.tag == '{http://www.mulesoft.org/schema/mule/core}error-handler']

    trigger = None
    steps = []
    sequence = 0

    for i, child in enumerate(children):
        ns_prefix, local = tag_ns(child.tag)
        classified = classify_step(child)

        # First element of a primary flow is likely the trigger
        if i == 0 and flow_type == 'primary' and classified['type'] in (
            'http_listener', 'scheduler_cron', 'scheduler_fixed',
            'file_listener', 'sftp_listener', 'jms_listener', 'vm_listener'
        ):
            trigger = classified
            continue

        # Check for scheduler inside a parent element
        if local == 'scheduler' or (ns_prefix == 'core' and local == 'scheduler'):
            scheduling = child.find('{http://www.mulesoft.org/schema/mule/core}scheduling-strategy')
            if scheduling is not None:
                cron_el = scheduling.find('{http://www.mulesoft.org/schema/mule/core}cron')
                fixed_el = scheduling.find('{http://www.mulesoft.org/schema/mule/core}fixed-frequency')
                if cron_el is not None:
                    classified.update({'type': 'scheduler_cron', 'expression': attr(cron_el, 'expression'), 'timezone': attr(cron_el, 'timeZone', default='UTC')})
                elif fixed_el is not None:
                    classified.update({'type': 'scheduler_fixed', 'frequency': attr(fixed_el, 'frequency'), 'time_unit': attr(fixed_el, 'timeUnit', default='MILLISECONDS')})
            if i == 0 and flow_type == 'primary':
                trigger = classified
                continue

        sequence += 1
        classified['sequence'] = sequence
        classified['_source_ref'] = f"{name}:step-{sequence}"
        steps.append(classified)

    # Parse error handler
    error_handler_el = el.find('{http://www.mulesoft.org/schema/mule/core}error-handler')
    error_handling = {'has_error_handler': False, 'strategies': []}
    if error_handler_el is not None:
        error_handling['has_error_handler'] = True
        for strategy in error_handler_el:
            ns_prefix, local = tag_ns(strategy.tag)
            error_type = attr(strategy, 'type', default='ANY')
            boomi_equiv = 'try_catch_rethrow' if 'propagate' in local else 'try_catch_continue'
            error_handling['strategies'].append({
                'error_type': error_type,
                'strategy': 'propagate' if 'propagate' in local else 'continue',
                'boomi_equivalent': boomi_equiv,
            })

    # Build boomi suggestions
    step_components = [s['boomi_step'] for s in steps if s['boomi_step'] != 'REVIEW_REQUIRED']
    connections_needed = list({s['boomi_component'].split(' + ')[0] for s in steps if 'connection' in s.get('boomi_component', '')})
    has_gaps = any(s.get('requires_review') for s in steps)
    complexity_scores = {'low': 1, 'medium': 2, 'high': 3}
    max_complexity = max((complexity_scores.get(s.get('complexity', 'low'), 1) for s in steps), default=1)
    complexity_label = {1: 'low', 2: 'medium', 3: 'high'}[max_complexity]

    pattern = infer_pattern(trigger, steps)
    safe_name = name.replace('-', '_').replace(' ', '_')
    boomi_name = f"MIG_MS_{safe_name[:40]}"

    return {
        'name': name,
        'source_name': name,
        'flow_type': flow_type,
        'trigger': trigger,
        'steps': steps,
        'error_handling': error_handling,
        'boomi_suggestions': {
            'process_name': boomi_name,
            'pattern': pattern,
            'trigger_component': trigger['boomi_step'] if trigger else None,
            'step_components': step_components,
            'connections_needed': connections_needed,
            'complexity': complexity_label,
            'manual_review_required': has_gaps,
        },
    }


def infer_pattern(trigger: Optional[dict], steps: list) -> str:
    if trigger is None:
        return 'subprocess'
    t = trigger.get('type', '')
    if t == 'http_listener':
        return 'request_reply_api'
    if t in ('scheduler_cron', 'scheduler_fixed'):
        has_sf = any(s['type'].startswith('salesforce') for s in steps)
        has_db = any(s['type'].startswith('db') for s in steps)
        if has_sf and has_db:
            return 'system_sync'
        return 'scheduled_batch'
    if t in ('file_listener', 'sftp_listener'):
        return 'file_processing'
    if t in ('jms_listener', 'vm_listener'):
        return 'event_driven'
    return 'unknown'


# ---------------------------------------------------------------------------
# Main analysis orchestration
# ---------------------------------------------------------------------------

def _iter_steps_recursive(steps):
    """Flatten all nested steps for gap collection."""
    for step in (steps or []):
        yield step
        for key in ('true_steps', 'false_steps', 'monitored_steps', 'loop_steps'):
            yield from _iter_steps_recursive(step.get(key, []))
        for branch in step.get('additional_branches', []):
            yield from _iter_steps_recursive(branch.get('steps', []))


_GAP_SEVERITY = {
    'scatter_gather': 'high',
    'async_scope':    'high',
    'batch_job':      'high',
    'custom':         'high',
    'transform':      'medium',   # may be enriched by LLM
    'try_scope':      'medium',
    'foreach':        'low',
}

_BEHAVIORAL_DIFFERENCES = {
    'scatter_gather': 'MuleSoft executes scatter-gather routes in parallel; Boomi Branch step is sequential. Throughput and latency will differ.',
    'async_scope':    'MuleSoft async scope is fire-and-forget with no return value; Boomi has no equivalent shape — requires a separate process.',
    'batch_job':      'MuleSoft batch has built-in chunking, error boundaries per record, and commit/rollback phases. Boomi approximation uses Data Process split + subprocess.',
    'transform':      'DataWeave transformation logic must be replicated in Boomi Map component or Groovy Data Process step.',
    'try_scope':      'MuleSoft error type hierarchy (e.g. DB:CONNECTIVITY) maps to Boomi Try/Catch but with different error granularity.',
    'custom':         'Step type is unknown — no automatic mapping available.',
}

_SUGGESTED_RESOLUTIONS = {
    'scatter_gather': 'Use Boomi Branch step (sequential). Accept if route order is independent; refactor if not.',
    'async_scope':    'Create a separate Boomi sub-process. Use Process Call with wait=false or Event Streams Produce for true async.',
    'batch_job':      'Use Data Process step (Split Documents) to chunk records, call a sub-process per chunk.',
    'transform':      'Run LLM enrichment (enrich_spec.py) to auto-generate field_mappings and Groovy equivalent.',
    'custom':         'Inspect source system docs; implement closest Boomi equivalent manually.',
}


def _gap_severity(step_type, step):
    if step.get('complexity') == 'high':
        return _GAP_SEVERITY.get(step_type, 'medium')
    return _GAP_SEVERITY.get(step_type, 'low')


def _behavioral_difference(step_type, step):
    return step.get('gap_note') or _BEHAVIORAL_DIFFERENCES.get(step_type, 'Behavior may differ — manual review required.')


def _suggested_resolution(step_type, step):
    return _SUGGESTED_RESOLUTIONS.get(step_type, f"Implement using {step.get('boomi_step', 'closest Boomi equivalent')}.")


def analyze_xml_file(xml_path: Path, connections: dict, integrations: list, gaps: list):
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Register namespaces to avoid re-parsing
    for child in root:
        ns_prefix, local = tag_ns(child.tag)
        name = attr(child, 'name')

        # Global configs
        result = parse_global_config(child)
        if result:
            conn_name, conn_data = result
            connections[conn_name] = conn_data
            continue

        # Flows
        if local == 'flow':
            flow_data = parse_flow(child, flow_type='primary')
            integrations.append(flow_data)
            for step in _iter_steps_recursive(flow_data['steps']):
                if step.get('requires_review'):
                    step_type = step['type']
                    severity = _gap_severity(step_type, step)
                    gaps.append({
                        'flow_name': flow_data['name'],
                        'step_sequence': step.get('sequence'),
                        'source_ref': step.get('_source_ref', ''),
                        'source_type': step_type,
                        'label': step.get('label', step_type),
                        'issue': step.get('gap_note', 'Requires manual review'),
                        'behavioral_difference': _behavioral_difference(step_type, step),
                        'resolution': f"Closest Boomi equivalent: {step.get('boomi_step', 'REVIEW_REQUIRED')}",
                        'suggested_resolution': _suggested_resolution(step_type, step),
                        'severity': severity,
                        'decision_required': severity in ('high', 'blocked'),
                        'auto_resolved': False,
                    })
            continue

        if local == 'sub-flow':
            flow_data = parse_flow(child, flow_type='sub_flow')
            integrations.append(flow_data)
            continue


def analyze_project(path: Path) -> dict:
    xml_files = []
    if path.is_file() and path.suffix == '.xml':
        xml_files = [path]
        project_name = path.stem
    elif path.is_dir():
        mule_dir = path / 'src' / 'main' / 'mule'
        search_dir = mule_dir if mule_dir.exists() else path
        xml_files = list(search_dir.rglob('*.xml'))
        project_name = path.name
    else:
        print(f"ERROR: Path not found: {path}", file=sys.stderr)
        sys.exit(1)

    if not xml_files:
        print(f"ERROR: No XML files found at {path}", file=sys.stderr)
        sys.exit(1)

    connections = {}
    integrations = []
    gaps = []

    for xml_file in sorted(xml_files):
        try:
            analyze_xml_file(xml_file, connections, integrations, gaps)
        except ET.ParseError as e:
            print(f"WARN: Could not parse {xml_file}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"WARN: Error processing {xml_file}: {e}", file=sys.stderr)

    # Compute overall complexity
    all_complexities = [f['boomi_suggestions']['complexity'] for f in integrations]
    scores = {'low': 1, 'medium': 2, 'high': 3}
    max_score = max((scores.get(c, 1) for c in all_complexities), default=1)
    overall_complexity = {1: 'low', 2: 'medium', 3: 'high'}[max_score]

    return {
        'schema_version': '1.0',
        'source_system': 'mulesoft',
        'source_version': '4.x',
        'analyzed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'project_name': project_name,
        'source_files_analyzed': [str(f) for f in xml_files],
        'summary': {
            'total_flows': len(integrations),
            'primary_flows': sum(1 for f in integrations if f['flow_type'] == 'primary'),
            'sub_flows': sum(1 for f in integrations if f['flow_type'] == 'sub_flow'),
            'total_connections': len(connections),
            'gaps_found': len(gaps),
            'overall_complexity': overall_complexity,
        },
        'connections': connections,
        'integrations': integrations,
        'gaps': gaps,
        'migration_notes': '',
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)

    input_path = Path(args[0])
    output_path = None
    project_name_override = None

    i = 1
    while i < len(args):
        if args[i] == '--output' and i + 1 < len(args):
            output_path = Path(args[i + 1])
            i += 2
        elif args[i] == '--project-name' and i + 1 < len(args):
            project_name_override = args[i + 1]
            i += 2
        else:
            i += 1

    spec = analyze_project(input_path)

    if project_name_override:
        spec['project_name'] = project_name_override

    if output_path is None:
        safe_name = spec['project_name'].replace(' ', '-').lower()
        output_path = Path('migration-specs') / f'{safe_name}.json'

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(spec, f, indent=2)

    # Print summary to stdout
    s = spec['summary']
    print(f"\n=== MuleSoft Analysis Complete ===")
    print(f"  Project:       {spec['project_name']}")
    print(f"  Flows:         {s['primary_flows']} primary, {s['sub_flows']} sub-flows")
    print(f"  Connections:   {s['total_connections']}")
    print(f"  Gaps found:    {s['gaps_found']}")
    print(f"  Complexity:    {s['overall_complexity']}")
    print(f"  Output:        {output_path}")

    if spec['gaps']:
        print(f"\n  Gaps requiring review:")
        for gap in spec['gaps']:
            print(f"    [{gap['severity'].upper()}] {gap['flow_name']}: {gap['issue']}")

    print(f"\n  Flows to migrate:")
    for integ in spec['integrations']:
        flag = ' ⚠ REVIEW' if integ['boomi_suggestions']['manual_review_required'] else ''
        print(f"    [{integ['flow_type']:10}] {integ['name']:50} -> {integ['boomi_suggestions']['pattern']}{flag}")
    print()


if __name__ == '__main__':
    main()
