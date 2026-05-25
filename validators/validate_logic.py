#!/usr/bin/env python3
"""
Logic Preservation Validator (Phase 2.5)

Reads a migration spec and produces a coverage report that answers:
  - What % of source logic was faithfully replicated?
  - Which steps need human verification?
  - What behavioral differences were accepted vs. flagged?
  - Where are the blind spots that could cause production failures?

Output:
  migration-specs/<project>_coverage_report.json   (machine-readable)
  migration-specs/<project>_review_checklist.md    (human-readable)

Usage:
    python validators/validate_logic.py migration-specs/my-project.json
    python validators/validate_logic.py migration-specs/my-project.json --strict
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── Step flattening ──────────────────────────────────────────────────────────

def iter_all_steps(steps, flow_name="", depth=0):
    """Yield (flow_name, depth, step) for every step recursively."""
    for step in (steps or []):
        yield flow_name, depth, step
        for key in ("true_steps", "false_steps", "monitored_steps", "loop_steps"):
            nested = step.get(key, [])
            if nested:
                yield from iter_all_steps(nested, flow_name, depth + 1)
        for branch in step.get("branches", []):
            if isinstance(branch, list):
                yield from iter_all_steps(branch, flow_name, depth + 1)


def iter_all_integrations(spec):
    for integration in spec.get("integrations", []):
        flow_name = integration.get("name", "unknown")
        for fn, depth, step in iter_all_steps(integration.get("steps", []), flow_name):
            yield flow_name, depth, step


# ─── Severity scoring ─────────────────────────────────────────────────────────

SEVERITY_DEDUCTIONS = {
    "blocked": 20.0,
    "high":    10.0,
    "medium":   5.0,
    "low":      2.0,
}

STEP_TYPE_RISK = {
    # MuleSoft / generic
    "transform":         "medium",  # Risk reduced to medium — high only when no field_mappings
    "scatter_gather":    "high",    # Parallel → sequential behavioral change
    "async_scope":       "high",    # No direct equivalent
    "batch_job":         "high",    # Complex mapping required
    "choice_router":     "low",     # Decision shapes map cleanly
    "db_select":         "low",     # SQL preserved verbatim
    "db_insert":         "low",
    "db_update":         "low",
    "db_stored_procedure": "medium",
    "http_request":      "medium",  # Auth setup varies
    "salesforce_query":  "low",     # Native connector available
    "try_scope":         "medium",  # Error strategy nuances
    "foreach":           "medium",  # Loop semantics differ slightly
    "custom":            "high",    # Unknown — always needs review
    # Oracle SOA / BPEL specific
    "oracle_ebs_api":    "high",    # EBS connector setup required; may need REST fallback
    "bpel_reply":        "low",     # Maps cleanly to Return Documents / WSS response
    "bpel_wait":         "high",    # No native Boomi timer — requires process split
    "bpel_receive":      "medium",  # Mid-flow receive (correlation) needs manual setup
    "bpel_invoke":       "medium",  # Generic fallback if adapter not resolved
    "error_handler":     "low",     # Catch branches map cleanly
    "trigger":           "medium",  # Trigger type may need manual config
    "oracle_ebs_event":  "high",    # EBS event subscription — no direct equivalent
    "jms_publish":       "low",     # Event Streams Produce maps cleanly
    "file_write":        "low",
    "file_read":         "low",
    "sftp_write":        "low",
    "sftp_read":         "low",
}


# ─── Coverage analysis ────────────────────────────────────────────────────────

class CoverageAnalyzer:
    def __init__(self, spec, strict=False):
        self.spec = spec
        self.strict = strict
        self.source_system = spec.get("source_system", "unknown")
        self.project_name = spec.get("project_name", "unknown")

    def analyze(self):
        all_steps = list(iter_all_integrations(self.spec))
        total = len(all_steps)

        fully_preserved = 0
        partially_preserved = 0
        needs_human = 0
        blocked = 0

        step_reports = []
        deductions = []
        checklist_items = []

        for flow_name, depth, step in all_steps:
            step_type = step.get("type", "custom")
            label = step.get("label", step_type)
            source_ref = step.get("_source_ref", "unknown")

            requires_review = step.get("requires_review", False)
            requires_human = step.get("requires_human", False)
            is_enriched = step.get("_enriched", False)
            severity = step.get("severity", STEP_TYPE_RISK.get(step_type, "low"))
            has_mappings = bool(step.get("field_mappings"))
            has_groovy = bool(step.get("groovy_equivalent"))
            has_behavioral_notes = bool(step.get("behavioral_notes"))
            decision_required = step.get("decision_required", False)
            auto_resolved = step.get("auto_resolvable", False) or (
                is_enriched and not requires_human and not requires_review
            )

            # Classify preservation status
            if severity == "blocked":
                status = "blocked"
                blocked += 1
            elif requires_human:
                status = "needs_human"
                needs_human += 1
            elif requires_review and not is_enriched:
                status = "partially_preserved"
                partially_preserved += 1
            elif requires_review and is_enriched and not auto_resolved:
                status = "partially_preserved"
                partially_preserved += 1
            else:
                status = "fully_preserved"
                fully_preserved += 1

            # Score deduction
            # Downgrade transform steps that have explicit field_mappings AND don't require review:
            # the mappings are known — implementation risk only, not structural risk.
            effective_severity = severity
            if step_type == "transform" and has_mappings and not requires_review:
                has_complex = step.get("has_complex_logic", False)
                effective_severity = "medium" if has_complex else "low"

            applies = (
                requires_human
                or effective_severity in ("high", "blocked")
                or (effective_severity == "medium" and requires_review and not is_enriched)
            )
            if effective_severity in SEVERITY_DEDUCTIONS and applies:
                deduction = SEVERITY_DEDUCTIONS.get(effective_severity, 2.0)
                deductions.append({
                    "flow": flow_name, "step": label, "severity": effective_severity,
                    "points_deducted": deduction, "reason": self._deduction_reason(step_type, step)
                })

            # Build checklist items
            checklist = self._build_checklist_item(
                flow_name, step, step_type, label, source_ref, status, has_mappings, has_groovy
            )
            if checklist:
                checklist_items.append(checklist)

            step_reports.append({
                "flow": flow_name,
                "source_ref": source_ref,
                "label": label,
                "type": step_type,
                "depth": depth,
                "status": status,
                "severity": severity,
                "enriched": is_enriched,
                "has_field_mappings": has_mappings,
                "has_groovy_equivalent": has_groovy,
                "has_behavioral_notes": has_behavioral_notes,
                "decision_required": decision_required,
                "auto_resolved": auto_resolved,
            })

        # Calculate preservation score
        total_deduction = sum(d["points_deducted"] for d in deductions)
        preservation_score = max(0.0, round(100.0 - total_deduction, 1))

        # Per-flow breakdown
        flow_breakdowns = self._flow_breakdown(step_reports)

        # Connections assessment
        connection_issues = self._assess_connections()

        return {
            "project": self.project_name,
            "source_system": self.source_system,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "strict_mode": self.strict,
            "preservation_score": preservation_score,
            "grade": self._grade(preservation_score),
            "summary": {
                "total_source_steps": total,
                "fully_preserved": fully_preserved,
                "partially_preserved": partially_preserved,
                "needs_human_review": needs_human,
                "blocked": blocked,
                "total_gaps": len(self.spec.get("gaps", [])),
                "connections_needing_setup": connection_issues["count"],
            },
            "score_breakdown": {
                "base_score": 100.0,
                "total_deducted": round(total_deduction, 1),
                "deductions": deductions,
            },
            "flows": flow_breakdowns,
            "connection_issues": connection_issues["details"],
            "human_review_checklist": checklist_items,
            "step_reports": step_reports,
        }

    def _deduction_reason(self, step_type, step):
        reasons = {
            "transform":          "Field mappings extracted but require wiring in Boomi Map editor",
            "scatter_gather":     "Parallel execution changed to sequential — throughput impact",
            "async_scope":        "Asynchronous behavior requires separate process architecture",
            "batch_job":          "Batch semantics require Data Process split + subprocess pattern",
            "custom":             "Step type has no direct Boomi equivalent",
            "oracle_ebs_api":     "Oracle EBS Adapter call — check native connector in Boomi account",
            "bpel_wait":          "Timer-based suspension not natively supported in Boomi",
            "bpel_receive":       "Mid-flow correlation receive requires manual setup",
        }
        return step.get("behavioral_difference") or reasons.get(step_type, "Requires manual verification")

    def _build_checklist_item(self, flow_name, step, step_type, label, source_ref, status, has_mappings, has_groovy):
        if status == "fully_preserved" and not step.get("decision_required"):
            return None

        priority = {"blocked": "CRITICAL", "needs_human": "HIGH",
                    "partially_preserved": "MEDIUM"}.get(status, "LOW")

        item = {
            "priority": priority,
            "flow": flow_name,
            "step": label,
            "source_ref": source_ref,
            "action_required": self._action_text(step_type, step, status, has_mappings, has_groovy),
            "boomi_shape": step.get("boomi_step", "TBD"),
            "decision_question": step.get("decision_question", ""),
            "suggested_implementation": step.get("suggested_implementation", ""),
        }
        return item

    def _action_text(self, step_type, step, status, has_mappings, has_groovy):
        if step_type == "transform":
            if has_mappings and has_groovy:
                return "Verify field mappings are complete and Groovy script produces correct output"
            if has_mappings:
                return "Create Boomi Map component using the extracted field_mappings; test with real data"
            return "DataWeave transform not mapped — manually create Boomi Map component and verify field-by-field"
        if step_type == "scatter_gather":
            return "Replace parallel scatter-gather with sequential Boomi Branch; verify order-independence of routes"
        if step_type == "async_scope":
            return "Implement as a separate Boomi sub-process; use Event Streams or Process Call with no-wait pattern"
        if step_type in ("http_request",):
            return f"Configure {step.get('boomi_connector', 'REST')} connector; verify auth setup: {step.get('auth_setup_notes', '')}"
        if status == "blocked":
            return f"BLOCKED: {step.get('behavioral_difference', 'No direct equivalent — requires architectural decision')}"
        if step.get("decision_required"):
            return f"Decision required: {step.get('decision_question', 'Review and decide implementation approach')}"
        return "Verify Boomi equivalent implements identical behavior; test with real integration data"

    def _flow_breakdown(self, step_reports):
        flows = {}
        for sr in step_reports:
            fn = sr["flow"]
            if fn not in flows:
                flows[fn] = {"name": fn, "steps": 0, "fully_preserved": 0,
                             "partially_preserved": 0, "needs_human": 0, "blocked": 0}
            flows[fn]["steps"] += 1
            status = sr["status"]
            flows[fn][status] = flows[fn].get(status, 0) + 1

        result = []
        for fn, data in flows.items():
            total = data["steps"]
            preserved = data.get("fully_preserved", 0)
            score = round(preserved / total * 100, 1) if total else 100.0
            result.append({**data, "flow_preservation_score": score})
        return result

    def _assess_connections(self):
        issues = []
        for name, conn in self.spec.get("connections", {}).items():
            if conn.get("requires_gui_setup"):
                issues.append({
                    "connection": name,
                    "type": conn.get("type"),
                    "issue": "OAuth / GUI setup required",
                    "notes": conn.get("notes", ""),
                })
            if conn.get("uses_env_vars"):
                env_vars = conn.get("env_vars", [])
                issues.append({
                    "connection": name,
                    "type": conn.get("type"),
                    "issue": "Environment variables must be configured",
                    "notes": f"Required vars: {', '.join(env_vars)}" if env_vars else conn.get("notes", ""),
                })
        return {"count": len(issues), "details": issues}

    @staticmethod
    def _grade(score):
        if score >= 95:  return "A"
        if score >= 85:  return "B"
        if score >= 70:  return "C"
        if score >= 50:  return "D"
        return "F"


# ─── Report writers ───────────────────────────────────────────────────────────

def write_json_report(report, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"Coverage report: {output_path}")


def write_markdown_checklist(report, output_path):
    lines = []
    lines.append(f"# Migration Logic Preservation Report")
    lines.append(f"**Project:** {report['project']}  ")
    lines.append(f"**Source:** {report['source_system']}  ")
    lines.append(f"**Generated:** {report['generated_at']}  ")
    lines.append(f"**Preservation Score:** {report['preservation_score']}% (Grade: {report['grade']})")
    lines.append("")

    s = report["summary"]
    lines.append("## Summary")
    lines.append(f"| Metric | Count |")
    lines.append(f"|---|---|")
    lines.append(f"| Total source steps | {s['total_source_steps']} |")
    lines.append(f"| Fully preserved | {s['fully_preserved']} |")
    lines.append(f"| Partially preserved | {s['partially_preserved']} |")
    lines.append(f"| Needs human review | {s['needs_human_review']} |")
    lines.append(f"| Blocked | {s['blocked']} |")
    lines.append(f"| Connections needing setup | {s['connections_needing_setup']} |")
    lines.append("")

    # Per-flow
    lines.append("## Per-Flow Scores")
    lines.append("| Flow | Steps | Score |")
    lines.append("|---|---|---|")
    for flow in report.get("flows", []):
        lines.append(f"| {flow['name']} | {flow['steps']} | {flow['flow_preservation_score']}% |")
    lines.append("")

    # Human review checklist
    checklist = [i for i in report.get("human_review_checklist", []) if i]
    if checklist:
        lines.append("## Human Review Checklist")
        lines.append("")

        for priority in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            items = [i for i in checklist if i["priority"] == priority]
            if not items:
                continue
            lines.append(f"### {priority} Priority")
            for item in items:
                lines.append(f"- [ ] **[{item['flow']}]** `{item['step']}` ({item.get('source_ref', '')})")
                lines.append(f"  - **Action:** {item['action_required']}")
                if item.get("decision_question"):
                    lines.append(f"  - **Decide:** {item['decision_question']}")
                if item.get("suggested_implementation"):
                    lines.append(f"  - **Suggestion:** {item['suggested_implementation']}")
                if item.get("boomi_shape") and item["boomi_shape"] != "TBD":
                    lines.append(f"  - **Boomi shape:** `{item['boomi_shape']}`")
                lines.append("")

    # Connection setup
    conn_issues = report.get("connection_issues", [])
    if conn_issues:
        lines.append("## Connection Setup Required")
        for ci in conn_issues:
            lines.append(f"- **{ci['connection']}** ({ci['type']}): {ci['issue']}")
            if ci.get("notes"):
                lines.append(f"  - {ci['notes']}")
        lines.append("")

    # Score deductions
    deductions = report.get("score_breakdown", {}).get("deductions", [])
    if deductions:
        lines.append("## Score Deductions")
        lines.append("| Flow | Step | Severity | Points |")
        lines.append("|---|---|---|---|")
        for d in deductions:
            lines.append(f"| {d['flow']} | {d['step']} | {d['severity']} | -{d['points_deducted']} |")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Review checklist: {output_path}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Logic preservation validator — scores migration coverage and generates review checklist."
    )
    parser.add_argument("spec_path", help="Path to migration-specs/*.json")
    parser.add_argument("--strict", action="store_true",
                        help="Treat partially-preserved steps as failures")
    parser.add_argument("--fail-below", type=float, default=None,
                        help="Exit with code 1 if preservation score < this value (for CI)")
    args = parser.parse_args()

    if not os.path.isfile(args.spec_path):
        print(f"ERROR: Spec not found: {args.spec_path}", file=sys.stderr)
        sys.exit(1)

    with open(args.spec_path, encoding="utf-8") as f:
        spec = json.load(f)

    analyzer = CoverageAnalyzer(spec, strict=args.strict)
    report = analyzer.analyze()

    spec_dir = os.path.dirname(os.path.abspath(args.spec_path))
    project = spec.get("project_name", "project")

    json_path = os.path.join(spec_dir, f"{project}_coverage_report.json")
    md_path = os.path.join(spec_dir, f"{project}_review_checklist.md")

    write_json_report(report, json_path)
    write_markdown_checklist(report, md_path)

    score = report["preservation_score"]
    grade = report["grade"]
    s = report["summary"]

    print(f"\n{'='*55}")
    print(f"Logic Preservation Score: {score}%  (Grade: {grade})")
    print(f"  Fully preserved : {s['fully_preserved']}/{s['total_source_steps']} steps")
    print(f"  Needs review    : {s['needs_human_review']} steps")
    print(f"  Blocked         : {s['blocked']} steps")
    print(f"  Human checklist : {len([i for i in report['human_review_checklist'] if i])} items")

    if args.fail_below is not None and score < args.fail_below:
        print(f"\nFAIL: Score {score}% is below threshold {args.fail_below}%", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
