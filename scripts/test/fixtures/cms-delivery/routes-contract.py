"""Compare migration and executable route seeds with the frozen delivery artifact."""
import json
import os
from pathlib import Path
import re
import subprocess

infra = Path(os.environ["XYNES_INFRA_REPO"])
contracts = Path(os.environ["XYNES_CONTRACTS_REPO"])
platform = Path(os.environ["XYNES_PLATFORM_CONFIG_REPO"])
artifact = json.loads((contracts / "contracts/cms-delivery.v1.json").read_text())
expected = [dict(artifact["route"], method=op["method"], pathPattern=op["path"],
                 actionKey=op["actionKey"]) for op in artifact["operations"].values()]
migration = infra / "supabase/migrations/20261005090000_seed_cms_delivery_routes.sql"
sql = re.sub(r"--[^\n]*", "", migration.read_text())
# The migration intentionally has one bounded INSERT/upsert and no tenant/key writes.
assert sql.strip().upper().startswith("INSERT INTO PLATFORM.ROUTES"), "unexpected target"
assert sql.count(";") == 1, "unexpected extra statement"
assert not re.search(r"\b(DELETE|DROP|TRUNCATE|ALTER)\b|workspace_api_key", sql, re.I)
assert re.search(r"ON CONFLICT\s*\(method,\s*path_pattern\)\s*DO UPDATE SET", sql, re.I)
rows = re.findall(r"\('([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*(true|false),\s*'([^']+)',\s*(true|false),\s*(true|false)\)", sql)
actual = [dict(method=m, pathPattern=p, serviceKey=s, targetPath=t,
               workspaceScoped=w == "true", actionKey=a, isPublic=u == "true")
          for m, p, s, t, w, a, u, enabled in rows if enabled == "true"]
assert actual == expected, "migration differs from frozen metadata or has disabled/extra routes"
assert len(rows) == 2
seed_source = platform / "src/seeds/routes.ts"
result = subprocess.run(["bun", "-e", '''const {routeSeeds} = await import(process.env.XYNES_ROUTE_SOURCE);
console.log(JSON.stringify(routeSeeds.filter(r => r.actionKey.startsWith("cms.delivery."))));'''],
    env={**os.environ, "XYNES_ROUTE_SOURCE": str(seed_source)}, check=True,
    capture_output=True, text=True)
assert json.loads(result.stdout) == expected, "platform seeds differ from frozen metadata"
print("PASS: two enabled private workspace delivery routes match A1 in SQL and executable seeds")
