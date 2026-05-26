#!/usr/bin/env python3
"""List all public -startos packages, cross-referenced with start9labs/Start9-Community ingestion."""

import subprocess
import json
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

INGESTION_ORGS = ["start9labs", "Start9-Community"]
SDK_PACKAGE = "@start9labs/start-sdk"
NOW = datetime.now(timezone.utc)


def gh(*args):
    result = subprocess.run(["gh"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def list_org_repos(org):
    out = gh(
        "api", f"orgs/{org}/repos",
        "--paginate",
        "-q", '[.[] | select(.name | endswith("-startos")) | {name, fork: .fork, parent_full_name: .parent.full_name}]',
    )
    if not out:
        return []
    repos = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            repos.extend(json.loads(line))
    return repos


def search_public_repos():
    repos = []
    page = 1
    while True:
        out = gh(
            "api", "search/repositories",
            "--method", "GET",
            "-f", "q=-startos in:name fork:false",
            "-f", f"per_page=100",
            "-f", f"page={page}",
            "-q", ".items[] | {fullName: .full_name, name: .name, fork: .fork}",
        )
        if not out:
            break
        batch = [json.loads(line) for line in out.splitlines() if line.strip()]
        if not batch:
            break
        repos.extend(r for r in batch if r["name"].endswith("-startos"))
        if len(batch) < 100:
            break
        page += 1
    return repos


def get_repo_info(full_name):
    """Return (sdk_version, pushed_at) for a repo."""
    # Fetch package.json and repo metadata in parallel-ish via two calls
    sdk = None
    pushed_at = None

    out = gh("api", f"repos/{full_name}", "-q", ".pushed_at")
    if out:
        pushed_at = out.strip()

    pkg_out = gh("api", f"repos/{full_name}/contents/package.json", "-q", ".content")
    if pkg_out:
        try:
            pkg = json.loads(base64.b64decode(pkg_out).decode())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            sdk = deps.get(SDK_PACKAGE)
        except Exception:
            pass

    return sdk, pushed_at


def age_days(pushed_at: str | None) -> float | None:
    if not pushed_at:
        return None
    try:
        dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        return (NOW - dt).days
    except Exception:
        return None


def format_date(pushed_at: str | None) -> str:
    if not pushed_at:
        return "—"
    try:
        dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "—"


def enrich(entry):
    upstream = entry["upstream_full_name"]
    sdk, pushed_at = get_repo_info(upstream)
    upstream_owner = upstream.split("/")[0]
    return {
        "name": entry["name"],
        "maintainer": upstream_owner,
        "sdk_version": sdk or "—",
        "pushed_at": pushed_at,
        "last_updated": format_date(pushed_at),
        "age_days": age_days(pushed_at),
    }


def bucket(r) -> int:
    d = r["age_days"]
    if d is None:
        return 2
    if d <= 183:
        return 0
    if d <= 365:
        return 1
    return 2


BUCKET_HEADERS = [
    "## Updated within 6 months",
    "## Updated within 1 year",
    "## Older / unknown",
]


def results_table(rows: list) -> str:
    rows = sorted(rows, key=lambda x: x["name"].lower())
    md = "| Package | Maintainer | SDK version | Last updated |\n"
    md += "| --- | --- | --- | --- |\n"
    for r in rows:
        md += f"| {r['name']} | {r['maintainer']} | {r['sdk_version']} | {r['last_updated']} |\n"
    return md


def main():
    by_name: dict[str, dict] = {}

    for org in INGESTION_ORGS:
        print(f"Fetching repos from {org}…", flush=True)
        for repo in list_org_repos(org):
            name = repo["name"]
            upstream = repo["parent_full_name"] if repo["fork"] and repo["parent_full_name"] else f"{org}/{name}"
            if name not in by_name:
                by_name[name] = {"name": name, "upstream_full_name": upstream, "orgs": []}
            if org not in by_name[name]["orgs"]:
                by_name[name]["orgs"].append(org)

    print("Searching public GitHub repos…", flush=True)
    for repo in search_public_repos():
        name = repo["name"]
        if name not in by_name:
            by_name[name] = {"name": name, "upstream_full_name": repo["fullName"], "orgs": []}

    entries = list(by_name.values())
    print(f"\nFound {len(entries)} unique packages. Fetching details…", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(enrich, e): e for e in entries}
        for i, future in enumerate(as_completed(futures), 1):
            r = future.result()
            results.append(r)
            print(f"  [{i}/{len(entries)}] {r['name']}  ({r['last_updated']})", flush=True)

    buckets: list[list] = [[], [], []]
    for r in results:
        buckets[bucket(r)].append(r)

    out_md = f"# StartOS Packages\n\n_{len(results)} packages · {NOW.strftime('%Y-%m-%d')} · sources: start9labs, Start9-Community, public GitHub_\n\n"
    for i, (header, rows) in enumerate(zip(BUCKET_HEADERS, buckets)):
        out_md += f"{header} ({len(rows)})\n\n"
        out_md += results_table(rows) + "\n"

    print("\n" + out_md)

    out_path = "README.md"
    with open(out_path, "w") as f:
        f.write(out_md)
    print(f"Saved to {out_path}", flush=True)


if __name__ == "__main__":
    main()
