#!/usr/bin/env python3
"""Read-only host inventory and deterministic convergence preview. Never applies."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys

STAGES = {"dev", "qa", "staging", "prod"}
LABEL_KEYS = {"io.xynes.managed", "io.xynes.owner", "io.xynes.environment", "io.xynes.cleanup"}
ALLOWED_PATHS = {f"{stage}{suffix}" for stage in STAGES for suffix in ["", "/compose", "/env", "/supabase", "/supabase/db"]}
PRESERVED_NETWORKS = {"caddy_net", *(f"xynes_{stage}_net" for stage in STAGES)}
IDENTIFIER = re.compile(r"(?:sha256:)?[a-f0-9]{64}\Z")


def require(condition):
    if not condition:
        raise ValueError("invalid inventory")


def timestamp(value):
    require(isinstance(value, str) and len(value) < 64)
    # Docker reports up to nanosecond precision; Python 3.9 accepts only six
    # fractional digits. Retention is measured in days, so truncate sub-micros.
    normalized = re.sub(r"(\.\d{6})\d{1,3}(?=Z|[+-]\d{2}:\d{2}$)", r"\1", value)
    result = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    require(result.tzinfo is not None)
    return result


def path_record(root, relative):
    require(relative in ALLOWED_PATHS)
    path = root
    for part in relative.split("/"):
        path = path / part
        require(not path.is_symlink())
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": relative, "exists": False}
    return {"path": relative, "exists": True, "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
            "uid": info.st_uid, "gid": info.st_gid, "mode": f"{stat.S_IMODE(info.st_mode):04o}", "symlink": False}


def safe_labels(raw):
    labels = raw if isinstance(raw, dict) else {}
    return {key: value for key, value in labels.items() if key in LABEL_KEYS and isinstance(value, str) and len(value) <= 80}


def capture(args, errors, category, json_result=False):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            raise ValueError("read unavailable")
        require(len(result.stdout.encode()) <= 1024 * 1024)
        return json.loads(result.stdout) if json_result else result.stdout.strip()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        errors.append(category)
        return None if json_result else ""


def collect(root):
    require(root.is_absolute() and root.is_dir() and not root.is_symlink())
    root = root.resolve(strict=True)
    errors = []
    records = [path_record(root, relative) for relative in sorted(ALLOWED_PATHS)]
    resources = []
    # Individual whitelisted fields form JSON; no Config.Env or mount source
    # is selected. Unknown labels are discarded before any report is emitted.
    templates = {
        "container": '{"id":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},"running":{{json .State.Running}},"created_at":{{json .Created}},"labels":{{json .Config.Labels}}}',
        "image": '{"id":{{json .Id}},"created_at":{{json .Created}},"labels":{{if .Config}}{{json (index .Config "Labels")}}{{else}}null{{end}},"size_bytes":{{json .Size}}}',
        "network": '{"id":{{json .Id}},"name":{{json .Name}},"created_at":{{json .Created}},"labels":{{json .Labels}},"containers":{{json .Containers}}}',
        "volume": '{"name":{{json .Name}},"created_at":{{json .CreatedAt}},"labels":{{json .Labels}}}',
    }
    for kind in ["container", "image", "network", "volume"]:
        listing = ["docker", kind, "ls", "--quiet"]
        if kind in {"container", "image"}:
            listing.append("--all")
        if kind != "volume":
            listing.append("--no-trunc")
        ids = capture(listing, errors, f"docker-{kind}s-unavailable")
        for identifier in ids.splitlines():
            if kind == "volume":
                require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", identifier))
            else:
                require(IDENTIFIER.fullmatch(identifier))
            raw = capture(["docker", kind, "inspect", "--format", templates[kind], identifier], errors, f"docker-{kind}-inspect-unavailable", True)
            if not isinstance(raw, dict):
                continue
            record = {"kind": kind, "id": raw.get("id", identifier), "name": raw.get("name", "").lstrip("/"),
                      "created_at": raw.get("created_at"), "labels": safe_labels(raw.get("labels")),
                      "running": bool(raw.get("running", False)), "image_id": raw.get("image_id"),
                      "references": list(raw.get("containers", {})), "size_bytes": raw.get("size_bytes", 0)}
            resources.append(record)
    group = capture(["getent", "group", "xynes"], errors, "xynes-group-unavailable")
    gid = int(group.split(":")[2]) if group else None
    users = []
    for stage in sorted(STAGES):
        name = f"xynes_{stage}_deploy"
        entry = capture(["getent", "passwd", name], [], "user-absent")
        if entry:
            fields = entry.split(":")
            users.append({"name": name, "uid": int(fields[2]), "gid": int(fields[3]), "docker_member": None})
    return {"schema_version": 1, "captured_at": datetime.now(timezone.utc).isoformat(), "root": str(root),
            "runtime": {"os": platform.system(), "kernel": platform.release(),
                        "docker": capture(["docker", "version", "--format", "{{.Server.Version}}"], errors, "docker-version-unavailable"),
                        "compose": capture(["docker", "compose", "version", "--short"], errors, "compose-version-unavailable")},
            "resources": resources, "paths": records, "users": users, "xynes_gid": gid,
            "retention": {"verified": False, "keep_days": 30, "current_images": [], "previous_images": []},
            "collection_errors": errors}


def plan(inventory):
    require(isinstance(inventory, dict) and inventory.get("schema_version") == 1)
    now = timestamp(inventory["captured_at"])
    require(isinstance(inventory.get("root"), str) and Path(inventory["root"]).is_absolute())
    retention = inventory["retention"]
    require(type(retention["keep_days"]) is int and 1 <= retention["keep_days"] <= 3650)
    paths = inventory["paths"]
    require(len({record["path"] for record in paths}) == len(paths))
    for record in paths:
        require(record["path"] in ALLOWED_PATHS and not record.get("symlink", False))
        require(not record.get("exists") or record.get("kind") == "directory")
    resources = inventory["resources"]
    seen = set()
    for resource in resources:
        kind, identifier = resource["kind"], resource["id"]
        require((kind, identifier) not in seen)
        seen.add((kind, identifier))
        if kind in {"image", "container", "network"}:
            require(IDENTIFIER.fullmatch(identifier))
        timestamp(resource["created_at"])
        labels = resource.get("labels", {})
        require(isinstance(labels, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()))
        if "io.xynes.environment" in labels:
            require(labels["io.xynes.environment"] in STAGES)
    gid = inventory["xynes_gid"]
    require(type(gid) is int and gid >= 0)
    actions = []
    desired = {"staging": "0755", "staging/compose": "0750", "staging/env": "0750", "staging/supabase": "0755"}
    for stage in ["dev", "qa", "prod"]:
        desired[f"{stage}/env"] = "0750"
    by_path = {record["path"]: record for record in paths}
    for relative, mode in sorted(desired.items()):
        record = by_path.get(relative, {"exists": False})
        if not record.get("exists") and not relative.startswith("staging"):
            continue
        if not record.get("exists") or (record.get("uid"), record.get("gid"), record.get("mode")) != (0, gid, mode):
            actions.append({"operation": "set_directory_metadata" if record.get("exists") else "create_directory",
                            "path": relative, "uid": 0, "gid": gid, "mode": mode, "recursive": False})
    if not any(r["kind"] == "network" and r["name"] == "xynes_staging_net" for r in resources):
        actions.append({"operation": "create_network", "name": "xynes_staging_net"})
    if not any(u["name"] == "xynes_staging_deploy" and u.get("docker_member") is False for u in inventory["users"]):
        actions.append({"operation": "provision_constrained_deploy_identity", "name": "xynes_staging_deploy"})
    protected = set(retention["current_images"] + retention["previous_images"])
    protected.update(r.get("image_id") for r in resources if r["kind"] == "container")
    candidates = []
    if retention["verified"] is True and not inventory["collection_errors"]:
        for resource in resources:
            labels = resource["labels"]
            owned = labels.get("io.xynes.managed") == "true" and labels.get("io.xynes.owner") == "xynes-infra" and labels.get("io.xynes.cleanup") == "eligible" and labels.get("io.xynes.environment") in STAGES
            age = (now - timestamp(resource["created_at"])).total_seconds() / 86400
            if resource["kind"] not in {"image", "container"} or not owned or age < retention["keep_days"]:
                continue
            if resource.get("running") or resource.get("references") or resource["id"] in protected or resource.get("image_id") in set(retention["current_images"] + retention["previous_images"]):
                continue
            candidates.append({"kind": resource["kind"], "id": resource["id"], "size_bytes": resource.get("size_bytes", 0), "requires_candidate_approval": True})
    result = {"schema_version": 1, "root": inventory["root"], "captured_at": inventory["captured_at"],
              "actions": actions, "cleanup_candidates": candidates, "apply_supported": False,
              "collection_errors": inventory["collection_errors"]}
    result["plan_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--root", default="/srv/xynes")
    parser.add_argument("--inventory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        require(args.collect != bool(args.inventory))
        require(args.collect or args.dry_run)
        if args.collect:
            result = collect(Path(args.root))
        else:
            file = Path(args.inventory)
            require(file.stat().st_size <= 16 * 1024 * 1024)
            result = plan(json.loads(file.read_text()))
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError):
        print("Host reconciliation refused; inventory or collection is incomplete", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
