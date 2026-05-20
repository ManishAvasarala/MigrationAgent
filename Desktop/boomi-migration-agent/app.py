#!/usr/bin/env python3
"""
Migration Agent — Streamlit UI
Run: streamlit run app.py
"""

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import streamlit as st

# ─── Constants ─────────────────────────────────────────────────────────────────

AGENT_DIR = Path(__file__).parent.resolve()

PLATFORMS = ["boomi", "mulesoft", "workato", "celigo", "webmethods"]
PLATFORM_LABELS = {
    "boomi":      "Boomi",
    "mulesoft":   "MuleSoft",
    "workato":    "Workato",
    "celigo":     "Celigo",
    "webmethods": "webMethods.io",
}

# Which platforms can act as source / target
SOURCE_PLATFORMS = ["boomi", "mulesoft", "workato", "celigo", "webmethods"]
TARGET_PLATFORMS = ["boomi", "workato", "celigo", "webmethods"]


# ─── Credential form helpers ────────────────────────────────────────────────────

def source_credential_form(platform: str) -> dict:
    """Render source credential fields and return {ENV_VAR: value, ...}."""
    env = {}

    if platform == "boomi":
        st.markdown("**Boomi source credentials**")
        env["BOOMI_API_URL"]    = st.text_input("API URL", value="https://api.boomi.com/api/rest/v1", key="src_boomi_url")
        env["BOOMI_ACCOUNT_ID"] = st.text_input("Account ID", key="src_boomi_account")
        env["BOOMI_USERNAME"]   = st.text_input("Username (email)", key="src_boomi_user")
        env["BOOMI_API_TOKEN"]  = st.text_input("API Token", type="password", key="src_boomi_token")
        env["BOOMI_VERIFY_SSL"] = "true"

    elif platform == "mulesoft":
        st.markdown("**MuleSoft source — point to local project files**")
        st.info("MuleSoft doesn't have a live pull step. Provide the path to your Mule project directory on this server.")
        env["_MULESOFT_SOURCE_DIR"] = st.text_input(
            "MuleSoft project directory path",
            placeholder="/path/to/mule-project/",
            key="src_ms_dir",
        )

    elif platform == "workato":
        st.markdown("**Workato source credentials**")
        env["WORKATO_API_TOKEN"] = st.text_input("API Token", type="password", key="src_wt_token")
        env["WORKATO_EMAIL"]     = st.text_input("Account email", key="src_wt_email")

    elif platform == "celigo":
        st.markdown("**Celigo source credentials**")
        env["CELIGO_API_TOKEN"] = st.text_input("API Token", type="password", key="src_cel_token")

    elif platform == "webmethods":
        st.markdown("**webMethods.io source credentials**")
        env["WMIO_TENANT_URL"] = st.text_input("Tenant URL", placeholder="https://mycompany.int-aws-us.webmethods.io", key="src_wmio_url")
        env["WMIO_USERNAME"]   = st.text_input("Username", key="src_wmio_user")
        env["WMIO_PASSWORD"]   = st.text_input("Password", type="password", key="src_wmio_pass")

    return env


def source_options_form(platform: str) -> dict:
    """Render source-specific options (folder name, etc). Returns extra CLI kwargs."""
    opts = {}

    if platform == "boomi":
        opts["boomi_folder"] = st.text_input(
            "Boomi folder to migrate",
            placeholder="e.g. CustomerAPI-Prod",
            key="src_boomi_folder",
        )

    elif platform == "workato":
        opts["workato_folder"] = st.text_input(
            "Workato folder name (leave blank for all)",
            key="src_wt_folder",
        )

    elif platform == "celigo":
        opts["celigo_integration"] = st.text_input(
            "Integration name (leave blank for all)",
            key="src_cel_integration",
        )

    elif platform == "webmethods":
        opts["wmio_project"] = st.text_input(
            "webMethods.io project name",
            key="src_wmio_project",
        )

    elif platform == "mulesoft":
        pass  # handled in source_credential_form

    return opts


def target_credential_form(platform: str) -> dict:
    """Render target credential fields and return {ENV_VAR: value, ...}."""
    env = {}

    if platform == "boomi":
        st.markdown("**Boomi target credentials**")
        env["BOOMI_API_URL"]        = st.text_input("API URL", value="https://api.boomi.com/api/rest/v1", key="tgt_boomi_url")
        env["BOOMI_ACCOUNT_ID"]     = st.text_input("Account ID", key="tgt_boomi_account")
        env["BOOMI_USERNAME"]       = st.text_input("Username (email)", key="tgt_boomi_user")
        env["BOOMI_API_TOKEN"]      = st.text_input("API Token", type="password", key="tgt_boomi_token")
        env["BOOMI_ENVIRONMENT_ID"] = st.text_input("Environment ID", key="tgt_boomi_env")
        env["BOOMI_TEST_ATOM_ID"]   = st.text_input("Atom ID", key="tgt_boomi_atom")
        env["BOOMI_TARGET_FOLDER"]  = st.text_input("Target Folder ID", key="tgt_boomi_folder")
        env["BOOMI_VERIFY_SSL"]     = "true"

    elif platform == "workato":
        st.markdown("**Workato target credentials**")
        env["WORKATO_API_TOKEN"] = st.text_input("API Token", type="password", key="tgt_wt_token")
        env["WORKATO_EMAIL"]     = st.text_input("Account email", key="tgt_wt_email")

    elif platform == "celigo":
        st.markdown("**Celigo target credentials**")
        env["CELIGO_API_TOKEN"] = st.text_input("API Token", type="password", key="tgt_cel_token")

    elif platform == "webmethods":
        st.markdown("**webMethods.io target credentials**")
        env["WMIO_TENANT_URL"] = st.text_input("Tenant URL", placeholder="https://mycompany.int-aws-us.webmethods.io", key="tgt_wmio_url")
        env["WMIO_USERNAME"]   = st.text_input("Username", key="tgt_wmio_user")
        env["WMIO_PASSWORD"]   = st.text_input("Password", type="password", key="tgt_wmio_pass")

    return env


# ─── Migration runner ──────────────────────────────────────────────────────────

def build_migrate_cmd(source: str, target: str, source_opts: dict, dest_name: str,
                      project: str, dry_run: bool, source_env: dict) -> list[str]:
    """Build the migrate.py CLI args list."""
    cmd = [sys.executable, str(AGENT_DIR / "migrate.py"),
           "--from", source, "--to", target]

    if source == "boomi" and source_opts.get("boomi_folder"):
        cmd += ["--boomi-folder", source_opts["boomi_folder"]]
    elif source == "mulesoft":
        src_dir = source_env.get("_MULESOFT_SOURCE_DIR", "")
        if src_dir:
            cmd += ["--source-dir", src_dir]
    elif source == "workato" and source_opts.get("workato_folder"):
        cmd += ["--source-dir", source_opts["workato_folder"]]
    elif source == "celigo" and source_opts.get("celigo_integration"):
        cmd += ["--source-dir", source_opts["celigo_integration"]]
    elif source == "webmethods" and source_opts.get("wmio_project"):
        cmd += ["--source-dir", source_opts["wmio_project"]]

    if project:
        cmd += ["--project", project]
    if dest_name:
        cmd += ["--dest-name", dest_name]
    if dry_run:
        cmd.append("--dry-run")

    return cmd


def stream_subprocess(cmd: list[str], env: dict, output_queue: queue.Queue):
    """Run cmd in a subprocess, push each output line to output_queue. Runs in a thread."""
    merged_env = {**os.environ, **{k: v for k, v in env.items() if not k.startswith("_") and v}}
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(AGENT_DIR),
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            output_queue.put(line)
        proc.wait()
        output_queue.put(None)  # sentinel: done
        output_queue.put(("__returncode__", proc.returncode))
    except Exception as e:
        output_queue.put(f"ERROR launching process: {e}\n")
        output_queue.put(None)
        output_queue.put(("__returncode__", 1))


# ─── Main UI ───────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Migration Agent", page_icon="🔄", layout="wide")
    st.title("Integration Migration Agent")
    st.caption("Migrate integration flows between any supported platforms.")

    # ── Source / Target columns ──────────────────────────────────────────────
    col_src, col_tgt = st.columns(2)

    with col_src:
        st.subheader("Source system")
        source = st.selectbox(
            "Platform",
            SOURCE_PLATFORMS,
            format_func=lambda p: PLATFORM_LABELS[p],
            key="source_platform",
        )
        src_env  = source_credential_form(source)
        src_opts = source_options_form(source)

    with col_tgt:
        st.subheader("Target system")
        target = st.selectbox(
            "Platform",
            TARGET_PLATFORMS,
            format_func=lambda p: PLATFORM_LABELS[p],
            key="target_platform",
        )
        tgt_env = target_credential_form(target)

    # ── Migration options ────────────────────────────────────────────────────
    st.divider()
    opt_col1, opt_col2, opt_col3 = st.columns([2, 2, 1])
    with opt_col1:
        project_name = st.text_input("Project name (optional)", placeholder="auto-generated if blank")
    with opt_col2:
        dest_name = st.text_input("Destination folder/project name (optional)", placeholder="MIG_<project>")
    with opt_col3:
        dry_run = st.checkbox("Dry run", help="Generate artifacts without pushing to the target platform")

    # ── Run button ───────────────────────────────────────────────────────────
    st.divider()
    run_col, _ = st.columns([1, 4])
    with run_col:
        run_clicked = st.button("▶ Run Migration", type="primary", use_container_width=True)

    # ── Output ───────────────────────────────────────────────────────────────
    if run_clicked:
        # Basic validation
        errors = []
        if source == target:
            errors.append("Source and target must be different platforms.")
        if source == "boomi" and not src_opts.get("boomi_folder") and not src_env.get("_MULESOFT_SOURCE_DIR"):
            errors.append("Boomi source requires a folder name.")
        if source == "mulesoft" and not src_env.get("_MULESOFT_SOURCE_DIR"):
            errors.append("MuleSoft source requires a project directory path.")

        if errors:
            for e in errors:
                st.error(e)
            return

        cmd = build_migrate_cmd(source, target, src_opts, dest_name, project_name, dry_run, src_env)
        combined_env = {**src_env, **tgt_env}

        st.subheader("Migration output")
        st.code(" ".join(cmd), language="bash")

        output_placeholder = st.empty()
        status_placeholder = st.empty()

        lines = []
        q: queue.Queue = queue.Queue()

        thread = threading.Thread(target=stream_subprocess, args=(cmd, combined_env, q), daemon=True)
        thread.start()

        returncode = None
        with st.spinner("Running migration…"):
            while True:
                try:
                    item = q.get(timeout=0.1)
                except queue.Empty:
                    output_placeholder.text_area("Log", value="".join(lines), height=400, label_visibility="collapsed")
                    continue

                if item is None:
                    break
                if isinstance(item, tuple) and item[0] == "__returncode__":
                    returncode = item[1]
                    continue

                lines.append(item)
                output_placeholder.text_area("Log", value="".join(lines), height=400, label_visibility="collapsed")

        # Drain any remaining returncode message
        while not q.empty():
            item = q.get_nowait()
            if isinstance(item, tuple) and item[0] == "__returncode__":
                returncode = item[1]

        output_placeholder.text_area("Log", value="".join(lines), height=400, label_visibility="collapsed")

        if returncode == 0:
            status_placeholder.success("✅ Migration completed successfully.")
        else:
            status_placeholder.error(f"❌ Migration failed (exit code {returncode}). See log above.")

        # Show spec file if it was produced
        project_slug = (project_name or "migration_project").lower().replace(" ", "_")
        spec_path = AGENT_DIR / "migration-specs" / f"{project_slug}.json"
        if spec_path.exists():
            with open(spec_path) as f:
                spec_content = f.read()
            with st.expander("📄 Migration spec (canonical JSON)"):
                st.code(spec_content, language="json")


if __name__ == "__main__":
    main()
