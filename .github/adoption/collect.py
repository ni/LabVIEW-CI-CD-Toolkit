#!/usr/bin/env python3
"""Build a public, aggregate GitHub adoption snapshot. Python standard library only."""
import argparse
import collections
import datetime as dt
import json
import os
import pathlib
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode

API = "https://api.github.com"
CANONICAL = "ni/labview-ci-cd-toolkit"
LEGACY = "elijah286/labview-ci-with-containers"
STAR_HEADER = "application/vnd.github.star+json"
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def request(path, token, accept="application/vnd.github+json"):
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "lvci-adoption"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response), 'rel="next"' in response.headers.get("Link", "")


def paged(path, token, accept="application/vnd.github+json"):
    delimiter = "&" if "?" in path else "?"
    page = 1
    while True:
        rows, more = request(f"{path}{delimiter}per_page=100&page={page}", token, accept)
        if not isinstance(rows, list):
            raise ValueError(f"Expected a list from {path}")
        yield from rows
        if not more:
            break
        page += 1


def day_counts(rows, field):
    return dict(collections.Counter(row[field][:10] for row in rows if row.get(field)))


def combine(meta, stars, forks, release_rows, pulls, archive, traffic, now, clients=None):
    """Merge only traffic and observations into the archive; replace recoverable event histories."""
    repo = meta["full_name"]
    if meta["private"]:
        raise ValueError("Adoption publication is available only for public repositories")
    if archive and (archive.get("schema") != 1 or archive.get("repo", "").lower() != repo.lower()):
        raise ValueError("The archive schema or repository does not match")
    releases = [r for r in release_rows if not r["draft"] and r.get("published_at")]
    assets = [a for r in releases for a in r["assets"]]
    latest = max(releases, key=lambda r: r["published_at"], default=None)
    saved = {kind: dict(archive.get("traffic", {}).get(kind, {})) for kind in ("views", "clones")}
    windows = dict(archive.get("traffic_windows", {}))
    clone_history = dict(archive.get("clone_window_history", {}))
    remember_clone_window(clone_history, windows.get("clones"))
    traffic_checked = dict(archive.get("traffic_checked", {}))
    timestamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    for kind, blob in traffic.items():
        traffic_checked[kind] = timestamp
        dates = []
        for row in blob[kind]:
            day = row["timestamp"][:10]
            # Redo overlapping observations each run and never infer zero for absent days.
            if meta["created_at"][:10] <= day < now.date().isoformat():
                saved[kind][day] = [row["count"], row["uniques"]]
                dates.append(day)
        # These GitHub-provided rolling-window totals have already deduplicated uniques.
        # They are shown only as exact-window context, never added across days or windows.
        if dates and len(dates) == len(blob[kind]) and max(dates) >= windows.get(kind, {}).get("to", ""):
            windows[kind] = {"from": min(dates), "to": max(dates), "count": blob["count"], "uniques": blob["uniques"]}
    remember_clone_window(clone_history, windows.get("clones"))
    client_history = dict(archive.get("clients", {}))
    if clients is not None:
        client_history[now.date().isoformat()] = clients
    return {
        "schema": 1, "repo": repo, "created": meta["created_at"][:10],
        "updated": timestamp,
        "current": {
            "stars": meta["stargazers_count"], "forks": meta["forks_count"], "watchers": meta["subscribers_count"],
            "assets": len(assets), "downloads": sum(a["download_count"] for a in assets),
        },
        "events": {
            "stars": day_counts(stars, "starred_at"), "forks": day_counts(forks, "created_at"),
            "releases": day_counts(releases, "published_at"), "merges": day_counts(pulls, "merged_at"),
        },
        "latest_release": {"tag": latest["tag_name"], "date": latest["published_at"][:10]} if latest else None,
        "traffic": saved, "traffic_windows": windows, "traffic_checked": traffic_checked,
        "clients": client_history, "clone_window_history": clone_history,
    }


def remember_clone_window(history, window):
    if window:
        history[f"{window['from']}/{window['to']}"] = window["uniques"]


def recover_clone_windows(repo, token):
    """Upgrade pre-history dashboards from their own published Git revisions once."""
    history = {}
    path = "adoption/data.json"
    revisions = list(paged(f"/repos/{repo}/commits?sha=gh-pages&path={path}", token))
    for revision in reversed(revisions):
        try:
            snapshot, _ = request(f"/repos/{repo}/contents/{path}?ref={revision['sha']}",
                                  token, "application/vnd.github.raw+json")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # A deletion commit has no file.
                continue
            raise
        if snapshot["repo"].lower() != repo.lower() or snapshot["schema"] != 1:
            raise ValueError("Unexpected historical archive")
        remember_clone_window(history, snapshot.get("traffic_windows", {}).get("clones"))
    return history


def verified_public_clients(token, fork_slugs):
    """Count public indexed topic repos with a catalog pointing to this toolkit's GitHub repo."""
    results, total, more = {}, None, True
    for page in range(1, 11):
        query = urlencode({"q": "topic:labview-ci fork:true is:public", "per_page": 100, "page": page})
        matches, more = request("/search/repositories?" + query, token)
        if total is None:
            total = matches["total_count"]
        if matches["incomplete_results"] or total != matches["total_count"] or total > 1000:
            raise ValueError("Client search was incomplete")
        results.update((item["id"], item) for item in matches["items"])
        if not more:
            break
    if more or len(results) != total:
        raise ValueError("Client search did not return every result")
    count = 0
    for item in results.values():
        slug = item["full_name"]
        if not REPO_NAME.fullmatch(slug) or item["private"] or slug.lower() in {CANONICAL, LEGACY} | fork_slugs:
            continue
        try:
            # Raw media type returns the catalog body. Archive only the overall count.
            catalog, _ = request(f"/repos/{slug}/contents/.github/labview-ci/catalog.json",
                                 token, "application/vnd.github.raw+json")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        except json.JSONDecodeError:
            continue
        if isinstance(catalog, dict) and isinstance(catalog.get("source"), dict):
            ref = catalog["source"].get("repo")
            count += isinstance(ref, str) and ref.lower() in (CANONICAL, LEGACY)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--archive", type=pathlib.Path)
    parser.add_argument("--input", type=pathlib.Path, help="Reproducible backfill from saved GitHub responses")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not REPO_NAME.fullmatch(args.repo):
        parser.error("--repo must be owner/repo")
    now = dt.datetime.now(dt.timezone.utc)
    archive = json.loads(args.archive.read_text()) if args.archive else {}
    issues = []
    clients = None
    if args.input:
        read = lambda name: json.loads((args.input / (name + ".json")).read_text())
        meta = read("repo")
        stars, forks, releases, pulls = ([row for page in read(name) for row in page]
                                         for name in ("stars", "forks", "releases", "pulls"))
        traffic = {kind: read(kind) for kind in ("views", "clones")}
        if (args.input / "clients.json").is_file():
            archive = {**archive, "schema": 1, "repo": meta["full_name"], "clients": read("clients")}
    else:
        token = os.environ.get("GH_TOKEN")
        root = "/repos/" + args.repo
        meta, _ = request(root, token)
        if meta["private"]:
            raise ValueError("Adoption publication is available only for public repositories")
        if archive and "clone_window_history" not in archive:
            archive["clone_window_history"] = recover_clone_windows(args.repo, token)
        stars = list(paged(root + "/stargazers", token, STAR_HEADER))
        forks = list(paged(root + "/forks", token))
        releases = list(paged(root + "/releases", token))
        pulls = list(paged(root + "/pulls?state=closed", token))
        traffic = {}
        traffic_token = os.environ.get("ADOPTION_TRAFFIC_TOKEN")
        if traffic_token:
            for kind in ("views", "clones"):
                try:
                    traffic[kind], _ = request(f"{root}/traffic/{kind}?per=day", traffic_token)
                except (OSError, ValueError) as exc:
                    issues.append(f"{kind} archive unchanged ({type(exc).__name__}); check traffic token access")
        else:
            issues.append("Traffic archive unchanged; set ADOPTION_TRAFFIC_TOKEN for daily collection")
        if args.repo.lower() == CANONICAL:
            try:
                clients = verified_public_clients(token, {f["full_name"].lower() for f in forks})
            except (OSError, ValueError):
                issues.append("Public client discovery incomplete; the last successful observation is retained")
    if meta["full_name"].lower() != args.repo.lower():
        raise ValueError("Response did not match the repository requested")
    result = combine(meta, stars, forks, releases, pulls, archive, traffic, now, clients)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    for issue in issues:
        print("::warning::" + issue)


if __name__ == "__main__":
    main()
