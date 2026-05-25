#!/usr/bin/env python3
"""
LLM Enrichment Pass (Phase 1.5)

Reads a migration spec produced by any source-system analyzer, finds every step
flagged requires_review=true, and calls Claude to fill in:
  - field_mappings   (for transform/DataWeave steps)
  - groovy_equivalent (Groovy 2.x script for Boomi Data Process)
  - behavioral_notes (documented semantic differences)
  - suggested_implementation (how to build this in Boomi)
  - severity / decision_required (for gap classification)

The enriched spec is written back to the same path so the generator can use it.

Usage:
    python enrichers/enrich_spec.py migration-specs/my-project.json
    python enrichers/enrich_spec.py migration-specs/my-project.json --dry-run
    python enrichers/enrich_spec.py migration-specs/my-project.json --step-limit 10

Required env var: ANTHROPIC_API_KEY
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── Anthropic client ─────────────────────────────────────────────────────────

def _get_client():
    try:
        import anthropic
    except ImportError:
        print("ERROR: pip install anthropic", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY") or _read_env_var("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in environment or .env", file=sys.stderr)
        sys.exit(1)

    return anthropic.Anthropic(api_key=api_key)


def _read_env_var(key):
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ─── Prompt templates ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert integration migration engineer. You analyze steps from integration
platforms (MuleSoft, Workato, Celigo, webMethods) and produce structured JSON that
describes how to faithfully replicate their logic in Boomi.

Always return ONLY valid JSON — no markdown fences, no commentary outside the JSON.
Be precise and complete. Logic preservation is the top priority.
"""

TRANSFORM_PROMPT = """\
Analyze this integration transformation step for migration to Boomi.

Source system: {source_system}
Flow: {flow_name}
Step label: {label}
Step type: {step_type}

Raw transformation script:
{raw_script}

Return JSON with exactly these fields:
{{
  "field_mappings": [
    {{
      "source": "source.field.path",
      "target": "targetFieldName",
      "type": "string|integer|boolean|datetime|decimal",
      "transformation": "passthrough|uppercase|lowercase|concat|split|dateformat|conditional|custom",
      "expression": "optional: the formula/expression if not passthrough",
      "notes": "optional: any important notes about this mapping"
    }}
  ],
  "has_complex_logic": true,
  "groovy_equivalent": "import java.util.Properties;\\nimport java.io.InputStream;\\nimport java.io.ByteArrayInputStream;\\nimport com.boomi.execution.ExecutionUtil;\\n\\nfor (int i = 0; i < dataContext.getDataCount(); i++) {{\\n    InputStream is = dataContext.getStream(i);\\n    Properties props = dataContext.getProperties(i);\\n    // ... full Boomi Groovy 2 script here ...\\n    dataContext.storeStream(new ByteArrayInputStream(result.bytes), props);\\n}}",
  "output_profile_hint": "json|xml|flatfile — suggested Boomi profile type for output",
  "boomi_map_feasible": true,
  "behavioral_notes": "Any behavioral differences or edge cases to watch for",
  "requires_human": false
}}
"""

HTTP_STEP_PROMPT = """\
Analyze this HTTP connector step for migration to Boomi.

Source system: {source_system}
Flow: {flow_name}
Step label: {label}
HTTP method: {method}
URL: {url}
Auth type: {auth_type}
Additional context: {context}

Return JSON:
{{
  "endpoint_purpose": "What this HTTP call does in business terms",
  "request_fields": ["field1", "field2"],
  "response_fields": ["field1", "field2"],
  "boomi_connector": "REST|HTTP|netsuitesdk|salesforce — recommended Boomi connector",
  "auth_setup_notes": "How to configure auth in Boomi",
  "behavioral_notes": "Any edge cases",
  "requires_human": false,
  "severity": "low|medium|high"
}}
"""

GAP_STEP_PROMPT = """\
Analyze this integration step that couldn't be automatically mapped.

Source system: {source_system}
Flow: {flow_name}
Step label: {label}
Step type: {step_type}
Known gap note: {gap_note}
Additional context: {context}

Return JSON:
{{
  "behavioral_difference": "Precise description of what behavior is lost or changed",
  "suggested_implementation": "How to implement this in Boomi",
  "boomi_components_needed": ["shape1", "shape2"],
  "auto_resolvable": false,
  "decision_required": true,
  "decision_question": "What the user needs to decide before migration can proceed",
  "severity": "low|medium|high|blocked",
  "requires_human": true,
  "behavioral_notes": "Additional notes"
}}
"""

CONDITION_PROMPT = """\
Analyze this conditional routing step for migration to Boomi.

Source system: {source_system}
Flow: {flow_name}
Step label: {label}
Condition expression: {condition}

Return JSON:
{{
  "condition_logic": "Plain-English description of what this condition checks",
  "boomi_decision_expression": "How to express this as a Boomi Decision shape DPP comparison",
  "dpp_name": "DPP_SUGGESTED_NAME — snake_case DPP to use for this comparison",
  "groovy_to_set_dpp": "Groovy snippet that reads the payload and sets the DPP before the Decision shape",
  "behavioral_notes": "Any edge cases or MuleSoft/Workato-specific behavior to note",
  "requires_human": false
}}
"""

SQL_PROMPT = """\
Analyze this database query step for migration to Boomi.

Source system: {source_system}
Flow: {flow_name}
Step label: {label}
SQL: {sql}

Return JSON:
{{
  "selected_fields": ["field1", "field2"],
  "where_conditions": ["condition1", "condition2"],
  "parameters": ["param1", "param2"],
  "joins": ["description of any joins"],
  "result_cardinality": "single|multiple",
  "boomi_operation_type": "GET|QUERY",
  "has_dynamic_sql": false,
  "behavioral_notes": "anything important",
  "requires_human": false
}}
"""


# ─── Step iterator ─────────────────────────────────────────────────────────────

def iter_all_steps(steps, flow_name=""):
    """Yield (flow_name, step) for every step recursively."""
    for step in steps:
        yield flow_name, step
        for key in ("true_steps", "false_steps", "monitored_steps", "loop_steps",
                    "loop_steps", "branches"):
            nested = step.get(key, [])
            if isinstance(nested, list):
                if nested and isinstance(nested[0], list):
                    for branch in nested:
                        yield from iter_all_steps(branch, flow_name)
                else:
                    yield from iter_all_steps(nested, flow_name)


def collect_review_steps(spec):
    """Return list of (flow_name, step) for all requires_review=True steps."""
    results = []
    for integration in spec.get("integrations", []):
        flow_name = integration.get("name", "unknown")
        for fn, step in iter_all_steps(integration.get("steps", []), flow_name):
            if step.get("requires_review"):
                results.append((flow_name, step))
    return results


# ─── Prompt builder ────────────────────────────────────────────────────────────

def build_prompt(flow_name, step, source_system):
    step_type = step.get("type", "custom")
    label = step.get("label", step_type)

    if step_type == "transform":
        raw = step.get("raw_script") or step.get("dataweave_script") or "(script not captured)"
        return TRANSFORM_PROMPT.format(
            source_system=source_system, flow_name=flow_name,
            label=label, step_type=step_type, raw_script=raw
        )

    if step_type == "http_request":
        return HTTP_STEP_PROMPT.format(
            source_system=source_system, flow_name=flow_name, label=label,
            method=step.get("http_method", step.get("method", "GET")),
            url=step.get("url", ""),
            auth_type=step.get("auth_type", "unknown"),
            context=json.dumps({k: v for k, v in step.items()
                                 if k not in ("type", "label", "url", "http_method")}, indent=2)
        )

    if step_type in ("choice_router", "choice_router_multi"):
        return CONDITION_PROMPT.format(
            source_system=source_system, flow_name=flow_name, label=label,
            condition=step.get("condition", "")
        )

    if step_type in ("db_select", "db_insert", "db_update", "db_delete", "db_stored_procedure"):
        return SQL_PROMPT.format(
            source_system=source_system, flow_name=flow_name, label=label,
            sql=step.get("sql", step.get("soql", ""))
        )

    # Generic gap step
    return GAP_STEP_PROMPT.format(
        source_system=source_system, flow_name=flow_name, label=label,
        step_type=step_type,
        gap_note=step.get("gap_note", step.get("behavioral_notes", "")),
        context=json.dumps({k: v for k, v in step.items()
                             if k not in ("type", "label") and not isinstance(v, list)}, indent=2)
    )


# ─── Claude call ──────────────────────────────────────────────────────────────

def call_claude(client, prompt, model="claude-opus-4-7"):
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def parse_json_response(text):
    """Extract and parse the JSON object from Claude's response."""
    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        # Try to find the first {...} block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


# ─── Step merger ──────────────────────────────────────────────────────────────

def merge_enrichment(step, enrichment, step_type):
    """Merge Claude's response into the step dict."""
    if not enrichment:
        return

    # Universal fields
    for field in ("behavioral_notes", "requires_human", "severity",
                  "decision_required", "decision_question", "auto_resolvable"):
        if field in enrichment:
            step[field] = enrichment[field]

    # Type-specific fields
    if step_type == "transform":
        for field in ("field_mappings", "has_complex_logic", "groovy_equivalent",
                      "output_profile_hint", "boomi_map_feasible"):
            if field in enrichment:
                step[field] = enrichment[field]
        if enrichment.get("field_mappings") and not enrichment.get("requires_human"):
            step["requires_review"] = False

    elif step_type == "http_request":
        for field in ("endpoint_purpose", "request_fields", "response_fields",
                      "boomi_connector", "auth_setup_notes"):
            if field in enrichment:
                step[field] = enrichment[field]

    elif step_type in ("choice_router", "choice_router_multi"):
        for field in ("condition_logic", "boomi_decision_expression",
                      "dpp_name", "groovy_to_set_dpp"):
            if field in enrichment:
                step[field] = enrichment[field]
        if enrichment.get("boomi_decision_expression") and not enrichment.get("requires_human"):
            step["requires_review"] = False

    elif step_type in ("db_select", "db_insert", "db_update", "db_delete"):
        for field in ("selected_fields", "where_conditions", "parameters",
                      "joins", "result_cardinality", "boomi_operation_type",
                      "has_dynamic_sql"):
            if field in enrichment:
                step[field] = enrichment[field]
        if not enrichment.get("requires_human"):
            step["requires_review"] = False

    else:
        for field in ("behavioral_difference", "suggested_implementation",
                      "boomi_components_needed", "decision_question"):
            if field in enrichment:
                step[field] = enrichment[field]

    step["_enriched"] = True


# ─── Enrichment orchestrator ───────────────────────────────────────────────────

def enrich_spec(spec_path, dry_run=False, step_limit=None, model="claude-opus-4-7"):
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)

    source_system = spec.get("source_system", "unknown")
    review_steps = collect_review_steps(spec)

    if not review_steps:
        print("No requires_review steps found — spec is already fully mapped.")
        return spec

    print(f"Found {len(review_steps)} step(s) requiring enrichment.")

    if step_limit:
        review_steps = review_steps[:step_limit]
        print(f"  (limited to {step_limit} steps by --step-limit)")

    if dry_run:
        print("\n[DRY RUN] Would enrich:")
        for flow_name, step in review_steps:
            print(f"  [{flow_name}] {step.get('type')} — {step.get('label', '')}")
        return spec

    client = _get_client()
    enriched_count = 0
    failed_count = 0

    for i, (flow_name, step) in enumerate(review_steps, 1):
        step_type = step.get("type", "custom")
        label = step.get("label", step_type)
        print(f"  [{i}/{len(review_steps)}] Enriching: [{flow_name}] {step_type} — {label}")

        try:
            prompt = build_prompt(flow_name, step, source_system)
            raw_response = call_claude(client, prompt, model=model)
            enrichment = parse_json_response(raw_response)
            merge_enrichment(step, enrichment, step_type)
            enriched_count += 1
            print(f"    ✓ enriched ({', '.join(k for k in enrichment if not k.startswith('_'))})")
        except Exception as e:
            print(f"    ✗ failed: {e}", file=sys.stderr)
            step["_enrichment_error"] = str(e)
            failed_count += 1

    # Update spec metadata
    spec["enrichment_metadata"] = {
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "steps_enriched": enriched_count,
        "steps_failed": failed_count,
        "total_reviewed": len(review_steps),
    }

    # Recount gaps after enrichment
    total_remaining = sum(
        1 for _, step in collect_review_steps(spec)
        if step.get("requires_human") or step.get("requires_review")
    )
    spec["summary"]["gaps_found"] = total_remaining

    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)

    print(f"\nEnrichment complete: {enriched_count} enriched, {failed_count} failed.")
    print(f"Remaining human-review items: {total_remaining}")
    print(f"Spec updated: {spec_path}")
    return spec


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LLM enrichment pass — fills in field_mappings, Groovy scripts, and gap assessments."
    )
    parser.add_argument("spec_path", help="Path to migration-specs/*.json file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be enriched without calling the API")
    parser.add_argument("--step-limit", type=int, default=None,
                        help="Limit number of steps to enrich (useful for testing)")
    parser.add_argument("--model", default="claude-opus-4-7",
                        help="Claude model to use (default: claude-opus-4-7)")
    args = parser.parse_args()

    if not os.path.isfile(args.spec_path):
        print(f"ERROR: Spec file not found: {args.spec_path}", file=sys.stderr)
        sys.exit(1)

    _load_dotenv()
    enrich_spec(args.spec_path, dry_run=args.dry_run,
                step_limit=args.step_limit, model=args.model)


def _load_dotenv():
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


if __name__ == "__main__":
    main()
