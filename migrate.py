#!/usr/bin/env python3
"""
Migration Orchestrator -- end-to-end pipeline for any A -> B integration migration.

Real-world usage:
    python migrate.py --from boomi --boomi-folder "MIG_CustomerAPI" --to workato
    python migrate.py --from boomi --boomi-folder "My Integrations" --to workato --project my-proj
    python migrate.py --from mulesoft --source-dir samples/mulesoft/customer-api/ --to workato

Pipeline:
    1. PULL   -- fetch source components (Boomi folder -> local XML, or point at existing files)
    2. ANALYZE -- run the right analyzer -> migration-specs/<project>.json
    3. GENERATE -- run the right generator -> target platform artifacts

No spec file needed as input -- the spec is produced automatically.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _load_dotenv():
    """Load .env from the project root into os.environ (setdefault — never overrides)."""
    env_file = Path(__file__).parent / ".env"
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

_load_dotenv()


# ─── Skill path detection ─────────────────────────────────────────────────────

def find_skill_path():
    """
    Locate the boomi-integration skill directory.
    Priority: BOOMI_SKILL_PATH env var > .env file > common install locations.
    """
    # 1. Environment variable
    p = os.environ.get("BOOMI_SKILL_PATH")
    if p and os.path.isdir(p):
        return p

    # 2. .env file
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("BOOMI_SKILL_PATH="):
                    p = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if os.path.isdir(p):
                        return p

    # 3. Common install locations
    candidates = [
        os.path.expanduser("~/.claude/plugins/cache/boomi-companion/bc-integration/*/skills/boomi-integration"),
        os.path.expanduser("~/.claude/plugins/cache/boomi-companion/*/skills/boomi-integration"),
    ]
    for pattern in candidates:
        matches = glob.glob(pattern)
        if matches:
            # Pick the most recent version
            return sorted(matches)[-1]

    return None


# ─── Shell script runner ──────────────────────────────────────────────────────

def _patched_env():
    """Return os.environ with the project-local bin/ prepended to PATH.
    This ensures jq.exe (and other bundled tools) are found when bash scripts
    are launched from a non-interactive subprocess (e.g. via Streamlit).
    """
    local_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
    env = os.environ.copy()
    # Convert Windows path to POSIX for bash on Windows (Git Bash / MSYS2)
    posix_bin = local_bin.replace("\\", "/")
    # Also keep the Windows form for native tools
    env["PATH"] = posix_bin + os.pathsep + local_bin + os.pathsep + env.get("PATH", "")
    return env


def run_script(script_path, args, cwd=None):
    """
    Run a bash script and return (stdout, returncode).
    Streams output live and also captures it.
    """
    cmd = ["bash", script_path] + args
    print(f"  $ bash {os.path.basename(script_path)} {' '.join(args)}")
    result = subprocess.run(cmd, cwd=cwd or os.getcwd(), env=_patched_env(),
                            capture_output=False, text=True)
    return result.returncode


def run_python(script_path, args, cwd=None):
    """Run a Python script."""
    cmd = [sys.executable, script_path] + args
    print(f"  $ python {os.path.basename(script_path)} {' '.join(args)}")
    result = subprocess.run(cmd, cwd=cwd or os.getcwd(), text=True)
    return result.returncode


# ─── Boomi pull phase ─────────────────────────────────────────────────────────

def get_latest_inventory(cwd):
    """Find the most recently written component_search_*.json file."""
    pattern = os.path.join(cwd, "active-development", "inventories", "component_search_*.json")
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def pull_boomi_folder(folder_name, skill_path, cwd, component_types="process"):
    """
    1. Search the Boomi folder for all processes (and optionally other types).
    2. Pull every component found to local active-development/.
    3. Pull each component's dependencies as well.
    Returns a list of pulled component IDs.
    """
    scripts = os.path.join(skill_path, "scripts")
    search_script = os.path.join(scripts, "boomi-component-search.sh")
    pull_script = os.path.join(scripts, "boomi-component-pull.sh")

    if not os.path.isfile(search_script):
        print(f"ERROR: Search script not found at {search_script}", file=sys.stderr)
        sys.exit(1)

    print(f"\n[PULL] Searching Boomi folder '{folder_name}' for components...")

    # Pull ALL component types in the folder (processes + their dependencies)
    all_types = "process,connector-settings,connector-action,transform.map,profile.json,profile.xml,profile.flatfile,profile.db,profile.edi"
    rc = run_script(search_script, ["--folder", folder_name, "--type", all_types], cwd=cwd)
    if rc != 0:
        print(f"ERROR: Component search failed (exit {rc})", file=sys.stderr)
        sys.exit(1)

    inventory_file = get_latest_inventory(cwd)
    if not inventory_file:
        print("ERROR: Search completed but no inventory file found.", file=sys.stderr)
        sys.exit(1)

    with open(inventory_file, encoding="utf-8") as f:
        inventory = json.load(f)

    records = inventory.get("records", [])
    if not records:
        print(f"WARNING: No components found in folder '{folder_name}'")
        return []

    print(f"  Found {len(records)} components in folder")

    # Pull each component
    pulled = []
    for rec in records:
        comp_id = rec.get("componentId") or rec.get("id")
        comp_name = rec.get("name", "unknown")
        comp_type = rec.get("type", "unknown")
        if not comp_id:
            continue
        print(f"  Pulling {comp_type}: {comp_name} ({comp_id})")
        rc = run_script(pull_script, ["--component-id", comp_id], cwd=cwd)
        if rc != 0:
            print(f"    WARNING: Pull failed for {comp_name} -- continuing", file=sys.stderr)
        else:
            pulled.append(comp_id)

    print(f"  Pulled {len(pulled)}/{len(records)} components")
    return pulled


# ─── Analyze phase ────────────────────────────────────────────────────────────

# Each analyzer entry: script path + how to build its CLI args.
# "positional" means source_dir is a positional arg (analyze_boomi, analyze_mulesoft).
# "source-dir" means it uses --source-dir <dir> flag (workato, celigo, webmethods).
ANALYZERS = {
    "boomi":       {"script": "analyzers/analyze_boomi.py",      "arg_style": "positional"},
    "mulesoft":    {"script": "analyzers/analyze_mulesoft.py",   "arg_style": "positional"},
    "workato":     {"script": "analyzers/analyze_workato.py",    "arg_style": "source-dir"},
    "celigo":      {"script": "analyzers/analyze_celigo.py",     "arg_style": "source-dir"},
    "webmethods":  {"script": "analyzers/analyze_webmethods.py", "arg_style": "source-dir"},
}


def run_analyze(source_system, source_dir, project_name, cwd):
    """Run the appropriate analyzer and return the spec file path."""
    entry = ANALYZERS.get(source_system)
    if not entry:
        print(f"ERROR: No analyzer for source system '{source_system}'. "
              f"Available: {', '.join(ANALYZERS)}", file=sys.stderr)
        sys.exit(1)

    analyzer_path = os.path.join(cwd, entry["script"])
    if not os.path.isfile(analyzer_path):
        print(f"ERROR: Analyzer not found: {analyzer_path}", file=sys.stderr)
        sys.exit(1)

    spec_path = os.path.join(cwd, "migration-specs", f"{project_name}.json")

    if entry["arg_style"] == "positional":
        # analyze_boomi.py <source_dir> --project <name> --output <path>
        analyzer_args = [source_dir, "--project", project_name, "--output", spec_path]
    elif source_system == "workato":
        if os.path.isdir(source_dir):
            # Local exported recipe JSON files
            analyzer_args = ["--source-dir", source_dir, "--project", project_name, "--output", spec_path]
        elif source_dir:
            # Non-empty, non-directory string = Workato folder name -> live API pull
            analyzer_args = ["--folder", source_dir, "--project", project_name, "--output", spec_path]
        else:
            # Blank -> pull all recipes via live API
            analyzer_args = ["--project", project_name, "--output", spec_path]
    else:
        # analyze_celigo.py / analyze_webmethods.py --source-dir <dir> ...
        analyzer_args = ["--source-dir", source_dir, "--project", project_name, "--output", spec_path]

    print(f"\n[ANALYZE] Running {os.path.basename(analyzer_path)}...")
    rc = run_python(analyzer_path, analyzer_args, cwd=cwd)
    if rc != 0:
        # If analysis failed but a spec already exists on disk, use it as a fallback.
        # This handles expired API tokens on re-runs without requiring manual intervention.
        if os.path.isfile(spec_path):
            print(f"\nWARNING: Analyzer exited with error but an existing spec was found.")
            print(f"  Using existing spec: {spec_path}")
            print(f"  To force re-analysis, refresh your API token and delete the spec file.")
        else:
            print(f"ERROR: Analyzer failed (exit {rc})", file=sys.stderr)
            sys.exit(1)

    if not os.path.isfile(spec_path):
        print(f"ERROR: Spec file not created at {spec_path}", file=sys.stderr)
        sys.exit(1)

    print(f"  Spec: {spec_path}")
    return spec_path


# ─── Generate phase ───────────────────────────────────────────────────────────

# Each generator entry: script path + the target-specific flag name for the
# "folder/project/integration" concept, and whether --dry-run is supported.
GENERATORS = {
    "workato":    {"script": "generators/generate_workato.py",    "dest_flag": "--folder"},
    "celigo":     {"script": "generators/generate_celigo.py",     "dest_flag": "--integration"},
    "webmethods": {"script": "generators/generate_webmethods.py", "dest_flag": "--project"},
    "boomi":      {"script": "generators/generate_boomi.py",      "dest_flag": "--project"},
    "mulesoft":   {"script": None},   # Future: generate_mulesoft.py
}

ENRICHER_SCRIPT  = "enrichers/enrich_spec.py"
VALIDATOR_SCRIPT = "validators/validate_logic.py"


def run_enrich(spec_path, cwd, step_limit=None, model="claude-opus-4-7"):
    """Phase 1.5: LLM enrichment of requires_review steps."""
    enricher = os.path.join(cwd, ENRICHER_SCRIPT)
    if not os.path.isfile(enricher):
        print(f"WARNING: Enricher not found at {enricher} — skipping enrichment", file=sys.stderr)
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set — skipping LLM enrichment")
        return

    args = [spec_path, "--model", model]
    if step_limit:
        args += ["--step-limit", str(step_limit)]

    print(f"\n[ENRICH] Running LLM enrichment pass ({model})...")
    rc = run_python(enricher, args, cwd=cwd)
    if rc != 0:
        print(f"WARNING: Enrichment failed (exit {rc}) — continuing with unenriched spec",
              file=sys.stderr)


def run_validate(spec_path, cwd, fail_below=None):
    """Phase 2.5: Logic preservation validation and coverage report."""
    validator = os.path.join(cwd, VALIDATOR_SCRIPT)
    if not os.path.isfile(validator):
        print(f"WARNING: Validator not found at {validator} — skipping validation", file=sys.stderr)
        return

    print(f"\n[VALIDATE] Computing logic preservation score...")
    args = [spec_path]
    if fail_below is not None:
        args += ["--fail-below", str(fail_below)]
    rc = run_python(validator, args, cwd=cwd)
    if rc != 0 and fail_below is not None:
        print(f"ERROR: Preservation score below threshold {fail_below}% — migration blocked",
              file=sys.stderr)
        sys.exit(1)


def run_generate(target_system, spec_path, dest_name, dry_run, cwd):
    """Run the appropriate generator."""
    entry = GENERATORS.get(target_system, {})
    generator = entry.get("script")

    if generator is None:
        available = [k for k, v in GENERATORS.items() if v.get("script")]
        print(f"ERROR: No generator yet for target '{target_system}'. "
              f"Available targets: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    generator_path = os.path.join(cwd, generator)
    if not os.path.isfile(generator_path):
        print(f"ERROR: Generator not found: {generator_path}", file=sys.stderr)
        sys.exit(1)

    dest_flag = entry.get("dest_flag", "--folder")
    gen_args = [spec_path, dest_flag, dest_name]
    if dry_run:
        gen_args.append("--dry-run")

    print(f"\n[GENERATE] Running {os.path.basename(generator_path)}...")
    rc = run_python(generator_path, gen_args, cwd=cwd)
    if rc != 0:
        print(f"ERROR: Generator failed (exit {rc})", file=sys.stderr)
        sys.exit(1)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="End-to-end integration migration: pull from source -> analyze -> generate for target.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Supported platforms: boomi, mulesoft, workato, celigo, webmethods

Examples:
  # Boomi folder -> Workato (live pull + push)
  python migrate.py --from boomi --boomi-folder "MIG_CustomerAPI" --to workato

  # Boomi folder -> Celigo
  python migrate.py --from boomi --boomi-folder "My Integrations" --to celigo

  # Boomi folder -> webMethods.io
  python migrate.py --from boomi --boomi-folder "My Folder" --to webmethods

  # MuleSoft project -> Workato
  python migrate.py --from mulesoft --source-dir samples/mulesoft/customer-api/ --to workato

  # Celigo exported JSON -> Workato
  python migrate.py --from celigo --source-dir path/to/celigo-exports/ --to workato

  # webMethods.io export -> Celigo
  python migrate.py --from webmethods --source-dir path/to/wmio-exports/ --to celigo

  # Dry run -- generate without pushing to target
  python migrate.py --from boomi --boomi-folder "My Folder" --to workato --dry-run

  # Skip pull and analyze (use existing spec)
  python migrate.py --from boomi --to workato --project my-proj --skip-pull --skip-analyze
""",
    )
    PLATFORMS = ["boomi", "mulesoft", "workato", "celigo", "webmethods"]
    parser.add_argument("--from", dest="source", required=True,
                        choices=PLATFORMS,
                        help="Source integration platform")
    parser.add_argument("--to", dest="target", required=True,
                        choices=PLATFORMS,
                        help="Target integration platform")

    # Source options
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--boomi-folder",
                              help="Boomi folder name or ID to pull processes from (for --from boomi)")
    source_group.add_argument("--source-dir",
                              help="Local directory of source files (skips the pull step)")

    parser.add_argument("--project", default=None,
                        help="Project name for spec and output file naming (default: folder or dir name)")
    parser.add_argument("--dest-name", default=None,
                        help="Name for the destination folder/project/integration on the target platform "
                             "(default: MIG_<project>)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate artifacts without pushing to the target platform")
    parser.add_argument("--skip-pull", action="store_true",
                        help="Skip the pull step (use already-downloaded active-development/ files)")
    parser.add_argument("--skip-analyze", action="store_true",
                        help="Skip the analyze step (use an existing spec in migration-specs/)")
    parser.add_argument("--skip-enrich", action="store_true",
                        help="Skip LLM enrichment pass (Phase 1.5) — no ANTHROPIC_API_KEY needed")
    parser.add_argument("--skip-validate", action="store_true",
                        help="Skip logic preservation validation (Phase 2.5)")
    parser.add_argument("--enrich-model", default="claude-opus-4-7",
                        help="Claude model for enrichment (default: claude-opus-4-7)")
    parser.add_argument("--enrich-limit", type=int, default=None,
                        help="Max steps to enrich (useful for cost control during testing)")
    parser.add_argument("--fail-below", type=float, default=None,
                        help="Fail migration if logic preservation score < this % (e.g. 80)")
    args = parser.parse_args()

    cwd = os.getcwd()

    # ── Validate source args ───────────────────────────────────────────────
    if args.source == "boomi" and not args.boomi_folder and not args.source_dir and not args.skip_pull:
        print("ERROR: --from boomi requires either --boomi-folder or --source-dir", file=sys.stderr)
        sys.exit(1)

    # Workato supports live API pull (no source-dir needed). Others still require local files.
    file_based_only_sources = ("mulesoft", "celigo", "webmethods")
    if args.source in file_based_only_sources and not args.source_dir and not args.skip_pull and not args.skip_analyze:
        print(f"ERROR: --from {args.source} requires --source-dir <path-to-exported-files>", file=sys.stderr)
        sys.exit(1)

    # ── Determine project name ─────────────────────────────────────────────
    project_name = args.project
    if not project_name:
        if args.boomi_folder:
            project_name = re.sub(r"[^\w-]", "_", args.boomi_folder).strip("_")
        elif args.source_dir:
            project_name = Path(args.source_dir.rstrip("/\\")).name
        else:
            project_name = "migration_project"
    project_name = project_name.lower().replace(" ", "_")

    dest_name = args.dest_name or f"MIG_{project_name}"

    print(f"Migration Agent")
    print(f"  Source  : {args.source}")
    print(f"  Target  : {args.target}")
    print(f"  Project : {project_name}")
    print(f"  Dest    : {dest_name}")
    print(f"  Dry run : {args.dry_run}")

    # ── PHASE 1: PULL ──────────────────────────────────────────────────────
    # For Workato live pull, source_dir may be None (blank = all recipes) or a folder name.
    # For all other non-Boomi sources, default to active-development/ if no --source-dir.
    if args.source == "workato":
        source_dir = args.source_dir or ""   # empty string = pull all recipes via API
    else:
        source_dir = args.source_dir or os.path.join(cwd, "active-development")

    if args.source == "boomi" and args.boomi_folder and not args.skip_pull:
        skill_path = find_skill_path()
        if not skill_path:
            print(
                "ERROR: Could not find boomi-integration skill. "
                "Set BOOMI_SKILL_PATH in .env to the skill directory path.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  Skill   : {skill_path}")

        pull_boomi_folder(args.boomi_folder, skill_path, cwd)
        source_dir = os.path.join(cwd, "active-development")

    elif args.skip_pull:
        print("\n[PULL] Skipped (--skip-pull)")

    # ── PHASE 2: ANALYZE ───────────────────────────────────────────────────
    spec_path = os.path.join(cwd, "migration-specs", f"{project_name}.json")

    if not args.skip_analyze:
        spec_path = run_analyze(args.source, source_dir, project_name, cwd)
    else:
        if not os.path.isfile(spec_path):
            print(f"ERROR: --skip-analyze set but spec not found at {spec_path}", file=sys.stderr)
            sys.exit(1)
        print(f"\n[ANALYZE] Skipped -- using existing spec: {spec_path}")

    # ── PHASE 1.5: ENRICH ─────────────────────────────────────────────────────
    if not args.skip_enrich and not args.dry_run:
        run_enrich(spec_path, cwd,
                   step_limit=args.enrich_limit,
                   model=args.enrich_model)
    elif args.skip_enrich:
        print("\n[ENRICH] Skipped (--skip-enrich)")

    # ── PHASE 2: GENERATE ─────────────────────────────────────────────────────
    run_generate(args.target, spec_path, dest_name, args.dry_run, cwd)

    # ── PHASE 2.5: VALIDATE ───────────────────────────────────────────────────
    if not args.skip_validate and not args.dry_run:
        run_validate(spec_path, cwd, fail_below=args.fail_below)
    elif args.skip_validate:
        print("\n[VALIDATE] Skipped (--skip-validate)")

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'=' * 50}")
    print(f"Migration complete: {args.source} -> {args.target}")
    print(f"  Project  : {project_name}")
    print(f"  Spec     : migration-specs/{project_name}.json")
    coverage_report = spec_path.replace(".json", "_coverage_report.json")
    checklist = spec_path.replace(".json", "_review_checklist.md")
    if os.path.isfile(coverage_report):
        print(f"  Coverage : migration-specs/{project_name}_coverage_report.json")
    if os.path.isfile(checklist):
        print(f"  Checklist: migration-specs/{project_name}_review_checklist.md")
    output_spec = spec_path.replace(".json", f"_{args.target}_output.json")
    if os.path.isfile(output_spec):
        print(f"  Output   : {os.path.relpath(output_spec)}")


if __name__ == "__main__":
    main()
