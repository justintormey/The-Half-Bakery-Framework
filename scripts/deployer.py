#!/usr/bin/env python3
"""Half Bakery Deploy Module — local S3 deployment, replacing GitHub Actions.

Usage:
  python3 deployer.py deploy <project>           Deploy a project to S3
  python3 deployer.py deploy <project> --dry-run  Show what would happen
  python3 deployer.py status                      Show deploy status
  python3 deployer.py list                        List configured deploy targets

All deployment config lives locally:
  - config/deploy-targets.json    → project → S3 path mapping (committed)
  - <project>/.local/deploy.json  → AWS profile, overrides (gitignored)
  - ~/.aws/credentials            → AWS access keys (standard AWS CLI)
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
CONFIG_DIR = REPO_DIR / "config"
DEPLOY_TARGETS_FILE = CONFIG_DIR / "deploy-targets.json"
PROJECTS_ROOT = Path.home() / "Documents" / "PROJECTS"
LOG_DIR = Path.home() / ".half-bakery" / "logs"
DEPLOY_LOG = LOG_DIR / "deploy.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("deployer")


def load_targets():
    if not DEPLOY_TARGETS_FILE.exists():
        log.error("Deploy targets not found: %s", DEPLOY_TARGETS_FILE)
        return {}
    with open(DEPLOY_TARGETS_FILE) as f:
        return json.load(f)


def resolve_project_dir(project_name):
    """Find the local directory for a project."""
    # Direct match
    candidate = PROJECTS_ROOT / project_name
    if candidate.is_dir():
        return candidate

    # Case-insensitive
    for child in PROJECTS_ROOT.iterdir():
        if child.is_dir() and child.name.lower() == project_name.lower():
            return child

    # Check demo.youruser.com sub-repos
    demo_dir = PROJECTS_ROOT / "demo.youruser.com"
    if demo_dir.is_dir():
        for child in demo_dir.iterdir():
            if child.is_dir() and project_name.lower() in child.name.lower():
                return child

    return None


def load_local_config(project_dir):
    """Load .local/deploy.json if it exists."""
    local_config = project_dir / ".local" / "deploy.json"
    if local_config.exists():
        with open(local_config) as f:
            return json.load(f)
    return {}


def apply_local_overlay(project_dir, deploy_dir):
    """Copy .local/ overlay files into the deploy staging directory.

    This merges PII/config from .local/ into the deployable output
    without those files ever entering git.
    """
    local_dir = project_dir / ".local"
    overlay_dir = local_dir / "overlay"

    if overlay_dir.is_dir():
        # Copy overlay files into deploy dir, overwriting
        for item in overlay_dir.rglob("*"):
            if item.is_file():
                rel = item.relative_to(overlay_dir)
                dest = deploy_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                log.info("  Overlay: %s", rel)


def deploy_project(project_name, dry_run=False):
    """Deploy a single project to S3."""
    targets = load_targets()

    if project_name not in targets:
        log.error("No deploy target configured for '%s'", project_name)
        log.info("Available: %s", ", ".join(sorted(targets.keys())))
        return False

    target = targets[project_name]
    project_dir = resolve_project_dir(target.get("local_dir", project_name))

    if not project_dir:
        log.error("Project directory not found for '%s'", project_name)
        return False

    log.info("Deploying %s from %s", project_name, project_dir)

    local_config = load_local_config(project_dir)
    s3_path = target["s3_path"]
    source = target.get("source", ".")
    build_cmd = target.get("build_command")
    excludes = target.get("exclude", [".git", ".github", ".local", "README.md", ".gitignore"])
    needs_overlay = target.get("needs_local_overlay", False)
    aws_profile = local_config.get("aws_profile", target.get("aws_profile", "default"))
    cloudfront_id = local_config.get("cloudfront_id", target.get("cloudfront_id"))

    source_dir = project_dir / source

    # Build step if needed
    if build_cmd:
        log.info("  Building: %s", build_cmd)
        if not dry_run:
            result = subprocess.run(
                build_cmd, shell=True, cwd=str(project_dir),
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                log.error("  Build failed: %s", result.stderr[:500])
                return False
            # Re-resolve source dir after build
            source_dir = project_dir / source

    # Always stage to a clean temp directory — avoids uploading .git/, .local/, etc.
    staging = Path(tempfile.mkdtemp(prefix=f"deploy-{project_name}-"))
    log.info("  Staging at %s", staging)

    # Copy deployable files to staging (excludes .git, .local, etc.)
    shutil.copytree(source_dir, staging, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        ".git", ".github", ".local", "__pycache__",
                        "node_modules", ".DS_Store", "*.pyc",
                        "README.md", "CHANGELOG.md", "history.md",
                        ".gitignore", "package.json", "package-lock.json",
                        "tests", "docs",
                    ))

    # Apply overlay if configured (merges .local/overlay/ into staging)
    if needs_overlay:
        apply_local_overlay(project_dir, staging)

    source_dir = staging

    # S3 sync from clean staging dir — no excludes needed, staging is already filtered
    s3_cmd = [
        "aws", "s3", "sync",
        str(source_dir), f"s3://{s3_path}",
        "--profile", aws_profile,
    ]
    # Only use --delete for project-specific paths (not shared root buckets)
    if target.get("delete", True):
        s3_cmd.append("--delete")

    if dry_run:
        s3_cmd.append("--dryrun")

    log.info("  S3 sync: %s → s3://%s %s", source_dir, s3_path,
             "(DRY RUN)" if dry_run else "")

    result = subprocess.run(s3_cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        log.error("  S3 sync failed: %s", result.stderr[:500])
        shutil.rmtree(staging, ignore_errors=True)
        return False

    if result.stdout.strip():
        for line in result.stdout.strip().splitlines()[:10]:
            log.info("    %s", line)

    # CloudFront invalidation
    if cloudfront_id and not dry_run:
        # Extract the path portion for invalidation
        path_prefix = s3_path.split("/", 1)[1] if "/" in s3_path else ""
        invalidation_path = f"/{path_prefix}/*" if path_prefix else "/*"

        log.info("  CloudFront invalidation: %s %s", cloudfront_id, invalidation_path)
        cf_result = subprocess.run([
            "aws", "cloudfront", "create-invalidation",
            "--distribution-id", cloudfront_id,
            "--paths", invalidation_path,
            "--profile", aws_profile,
        ], capture_output=True, text=True, timeout=30)

        if cf_result.returncode != 0:
            log.warning("  CloudFront invalidation failed: %s", cf_result.stderr[:200])

    # Clean up staging
    shutil.rmtree(staging, ignore_errors=True)

    # Log the deployment
    _log_deploy(project_name, s3_path, dry_run)

    status = "DRY RUN" if dry_run else "SUCCESS"
    log.info("  Deploy %s: %s → s3://%s", status, project_name, s3_path)
    return True


def _log_deploy(project_name, s3_path, dry_run):
    """Append deployment record to deploy.log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "project": project_name,
        "s3_path": s3_path,
        "dry_run": dry_run,
    }
    with open(DEPLOY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def show_status():
    """Show deploy status for all configured targets."""
    targets = load_targets()
    print(f"{'Project':25s} {'S3 Path':40s} {'Local Dir':30s} {'Overlay'}")
    print("-" * 105)
    for name, target in sorted(targets.items()):
        project_dir = resolve_project_dir(target.get("local_dir", name))
        exists = "✓" if project_dir else "✗"
        has_overlay = ""
        if project_dir and (project_dir / ".local").is_dir():
            has_overlay = "✓ .local/"
        print(f"{exists} {name:23s} {target['s3_path']:40s} {str(project_dir or '?'):30s} {has_overlay}")


def list_targets():
    """List all configured deploy targets."""
    targets = load_targets()
    for name in sorted(targets.keys()):
        print(name)


def main():
    parser = argparse.ArgumentParser(description="Half Bakery Deploy Module")
    sub = parser.add_subparsers(dest="command")

    deploy_p = sub.add_parser("deploy", help="Deploy a project to S3")
    deploy_p.add_argument("project", help="Project name or 'all'")
    deploy_p.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="Show deploy status")
    sub.add_parser("list", help="List deploy targets")

    args = parser.parse_args()

    if args.command == "deploy":
        if args.project == "all":
            targets = load_targets()
            for name in sorted(targets.keys()):
                deploy_project(name, dry_run=args.dry_run)
        else:
            success = deploy_project(args.project, dry_run=args.dry_run)
            sys.exit(0 if success else 1)
    elif args.command == "status":
        show_status()
    elif args.command == "list":
        list_targets()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
