"""Compare pinned real artifacts, schema behavior and route metadata; no database."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from pinned_inputs import load_pinned_file, compare_contracts


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise ValueError("Missing pinned A5 input: " + name)
    return value


def main():
    mode = os.environ.get("CMS_DELIVERY_PARITY_MODE", "full")
    if mode not in ("full", "backend-only"):
        raise ValueError("Unsupported parity mode")
    repos = {}
    for name in ("CONTRACTS", "CMS", "PLATFORM_CONFIG", "INFRA"):
        repos[name] = (Path(required("XYNES_" + name + "_REPO")).resolve(), required("XYNES_" + name + "_REVISION"))
    if mode == "full":
        repos["FRONTEND"] = (Path(required("XYNES_FRONTEND_REPO")).resolve(), required("XYNES_FRONTEND_REVISION"))
    canonical = load_pinned_file(*repos["CONTRACTS"], "contracts/cms-delivery.v1.json")
    compare_contracts(canonical, canonical)
    artifact = json.loads(canonical)
    if mode == "full":
        mirror = load_pinned_file(*repos["FRONTEND"], os.environ.get("XYNES_FRONTEND_CONTRACT_PATH", "src/features/content-integrations/fixtures/cms-delivery.v1.json"))
        compare_contracts(canonical, mirror)
    schema_path = "src/actions/schemas/content-delivery.ts"
    load_pinned_file(*repos["CMS"], schema_path)
    load_pinned_file(*repos["PLATFORM_CONFIG"], "src/seeds/routes.ts")
    load_pinned_file(*repos["INFRA"], "supabase/migrations/20261005090000_seed_cms_delivery_routes.sql")
    # Reuse the real-artifact A4 route validator; no extra mirrored route constants.
    route_checker = repos["INFRA"][0] / "scripts/test/fixtures/cms-delivery/routes-contract.py"
    load_pinned_file(*repos["INFRA"], "scripts/test/fixtures/cms-delivery/routes-contract.py")
    subprocess.run([sys.executable, "-B", str(route_checker)], check=True, capture_output=True, env=os.environ)
    directory = artifact["operations"]["directory"]
    entry = artifact["operations"]["entry"]
    identifier = "11111111-1111-4111-8111-111111111111"
    cases = []
    def case(target, payload, success, data=None):
        row = {"target": target, "payload": payload, "success": success}
        if data is not None:
            row["data"] = data
        cases.append(row)
    default = {"directoryId": identifier, "sortBy": directory["defaultSortBy"], "sortDirection": directory["defaultSortDirection"], "limit": directory["limit"]["default"], "offset": directory["offset"]["default"], "fields": directory["fields"]}
    case("directory", {"directoryId": identifier}, True, default)
    case("entry", {"entryId": identifier}, True, {"entryId": identifier, "fields": entry["fields"]})
    for name in ("limit", "offset"):
        for value in (directory[name]["min"], directory[name]["max"]):
            case("directory", {"directoryId": identifier, name: value}, True)
        for value in (directory[name]["min"] - 1, directory[name]["max"] + 1, 1.5, "2"):
            case("directory", {"directoryId": identifier, name: value}, False)
    for name in ("sortBy", "sortDirection"):
        for value in directory[name]:
            case("directory", {"directoryId": identifier, name: value}, True)
        case("directory", {"directoryId": identifier, name: "unsupported"}, False)
    for value, success in (("x" * directory["searchMaxLength"], True), ("x" * (directory["searchMaxLength"] + 1), False), ("", False)):
        case("directory", {"directoryId": identifier, "search": value}, success)
    for target, fields in (("directory", directory["fields"]), ("entry", entry["fields"])):
        key = "directoryId" if target == "directory" else "entryId"
        for field in fields:
            case(target, {key: identifier, "fields": field}, True, {**(default if target == "directory" else {"entryId": identifier}), "fields": ["id"] if field == "id" else ["id", field]})
        for selector in ("id,id", "", "unknown", "x" * (artifact["fieldsMaxLength"] + 1)):
            case(target, {key: identifier, "fields": selector}, False)
        case(target, {key: "invalid"}, False)
        case(target, {key: identifier, "preview": True}, False)
    case("directory", {"directoryId": identifier, "fields": "body"}, False)
    case("entry", {"entryId": identifier, "limit": 1}, False)
    probe = Path(__file__).with_name("schema-probe.ts")
    environment = {**os.environ, "XYNES_A5_SCHEMA_SOURCE": str(repos["CMS"][0] / schema_path), "XYNES_A5_SCHEMA_CASES": json.dumps(cases)}
    result = subprocess.run(["bun", str(probe)], env=environment, check=True, capture_output=True, text=True)
    actual = json.loads(result.stdout)
    if actual != {"checksPassed": len(cases), "directoryFields": directory["fields"], "entryFields": entry["fields"]}:
        raise ValueError("Schema exports differ from frozen contract")
    print(json.dumps({"mode": mode, "schemaChecks": len(cases), "artifactSha256": hashlib.sha256(canonical).hexdigest(), "revisions": {name: revision for name, (_, revision) in repos.items()}}))
    print("PASS: A5 pinned full contract parity" if mode == "full" else "BACKEND-ONLY PASS: B generated mirror/copy join remains pending")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("ERROR: " + (str(error) if isinstance(error, ValueError) else "A5 parity prerequisite/probe failed"), file=sys.stderr)
        raise SystemExit(1)
