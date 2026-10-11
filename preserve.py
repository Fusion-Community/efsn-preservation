import argparse
import hashlib
import http.client
import json
import os
import subprocess
import tempfile
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


REPOSITORIES = {
    "upstream": "https://github.com/FUSIONFoundation/efsn.git",
    "community": "https://github.com/Fusion-Community/efsn.git",
    "gateway": "https://github.com/algorexxx/fsn-efsn.git",
    "upstream-wiki": "https://github.com/FUSIONFoundation/efsn.wiki.git",
    "dcrm-go-wiki": "https://github.com/FUSIONFoundation/dcrm-go.wiki.git",
}
RELEASE_REPOSITORIES = {
    "upstream": "FUSIONFoundation/efsn",
    "myfusionwallet": "FUSIONFoundation/myfusionwallet",
    "web3-fusion-extend": "FUSIONFoundation/web3-fusion-extend",
}
RELEASE_FILE_LIMIT = 2 * 1024**3


def collect_snapshot(destination, captured_at):
    destination.mkdir()
    repositories = []
    for name, url in REPOSITORIES.items():
        print(f"Preserving {name}", flush=True)
        repositories.append(preserve_repository(name, url, destination))
    releases = [
        preserve_releases(destination, name, repository)
        for name, repository in RELEASE_REPOSITORIES.items()
    ]
    write_json(destination / "manifest.json", {
        "captured_at": captured_at,
        "workflow_commit": os.environ.get("GITHUB_SHA"),
        "repositories": repositories,
        "releases": releases,
    })
    asset_count = sum(len(source["assets"]) for source in releases)
    write_snapshot_notes(destination, captured_at, len(repositories), asset_count)
    write_checksums(destination)


def preserve_repository(name, url, destination):
    bundle = destination / f"{name}.bundle"
    with tempfile.TemporaryDirectory(prefix="efsn-preservation-") as directory:
        source = Path(directory) / "source.git"
        restored = Path(directory) / "restored.git"
        run_git("clone", "--quiet", "--mirror", url, str(source))
        refs = read_refs(source)
        head = run_git("symbolic-ref", "HEAD", cwd=source).strip()
        run_git("fsck", "--full", "--no-dangling", cwd=source)
        run_git("bundle", "create", str(bundle), "--all", cwd=source)
        run_git("clone", "--quiet", "--mirror", str(bundle), str(restored))
        run_git("fsck", "--full", "--no-dangling", cwd=restored)
        if read_refs(restored) != refs:
            raise ValueError(f"Restored refs differ for {name}")
    return {"url": url, "bundle": bundle.name, "head_ref": head, "refs": refs}


def read_refs(repository):
    lines = run_git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repository)
    return dict(line.split(" ", 1) for line in lines.splitlines())


def run_git(*arguments, cwd=None):
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, encoding="utf-8",
    ).stdout


def preserve_releases(destination, name, repository):
    releases = read_pages(f"/repos/{repository}/releases")
    assets = []
    for release in releases:
        release_id = require_positive_id(release["id"])
        release["assets"] = read_pages(
            f"/repos/{repository}/releases/{release_id}/assets"
        )
        for asset in release["assets"]:
            asset_id = require_positive_id(asset["id"])
            filename = f"{name}-release-{release_id}-asset-{asset_id}.bin"
            print(f"Preserving {repository} release asset {asset_id}", flush=True)
            download_asset(asset, destination / filename, repository)
            assets.append({"release_id": release_id, "asset_id": asset_id, "file": filename})
    metadata = f"{name}-releases.json"
    write_json(destination / metadata, releases)
    return {"repository": repository, "metadata": metadata, "assets": assets}


def require_positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Expected a positive GitHub identifier")
    return value


def read_pages(endpoint):
    result = []
    page = 1
    while True:
        entries = read_github_json(f"{endpoint}?per_page=100&page={page}")
        if not isinstance(entries, list):
            raise ValueError(f"Expected a GitHub list from {endpoint}")
        result.extend(entries)
        if len(entries) < 100:
            return result
        page += 1


def read_github_json(endpoint):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Fusion-Community-efsn-preservation",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with closing(http.client.HTTPSConnection("api.github.com", timeout=120)) as connection:
        connection.request("GET", endpoint, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"GitHub API returned {response.status} for {endpoint}")
        return json.load(response)


def download_asset(asset, destination, repository):
    url = asset["browser_download_url"]
    parsed = urllib.parse.urlsplit(url)
    prefix = f"/{repository}/releases/download/".lower()
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or not parsed.path.lower().startswith(prefix)):
        raise ValueError("Release asset is outside the expected public GitHub repository")
    if type(asset["size"]) is not int or not 0 <= asset["size"] < RELEASE_FILE_LIMIT:
        raise ValueError("Release asset size is invalid or requires splitting")
    if asset["state"] != "uploaded":
        raise ValueError("Upstream asset is not fully uploaded")
    request = urllib.request.Request(url, headers={"User-Agent": "efsn-preservation"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("xb") as target:
        copy_asset(response, target, asset["size"], asset.get("digest"))


def copy_asset(source, target, expected_size, expected_digest):
    digest = hashlib.sha256()
    total = 0
    while chunk := source.read(1024 * 1024):
        total += len(chunk)
        if total > expected_size:
            raise ValueError("Downloaded asset exceeds the upstream size")
        digest.update(chunk)
        target.write(chunk)
    if total != expected_size:
        raise ValueError("Downloaded asset is truncated")
    if expected_digest and expected_digest != f"sha256:{digest.hexdigest()}":
        raise ValueError("Downloaded asset does not match the upstream digest")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_snapshot_notes(destination, captured_at, repository_count, asset_count):
    (destination / "SNAPSHOT.md").write_text(
        f"# Preservation snapshot\n\nCaptured at {captured_at}.\n\n"
        f"{repository_count} Git bundles and {asset_count} upstream release downloads. "
        "Each bundle was restored without contacting GitHub, checked with git fsck, "
        "and compared with the source ref inventory. SHA256SUMS covers every saved file.\n\n"
        "manifest.json maps bundles, release repositories, metadata files and asset IDs. "
        "Each *-releases.json keeps original filenames, release notes, URLs and any "
        "upstream hashes. Restore an asset's original "
        "filename using that mapping. Locally computed hashes prove copy integrity, not "
        "the authenticity or safety of the original binaries.\n\n"
        "This is a preservation copy, not a new efsn software release. Licenses and notices "
        "remain those of the original projects.\n\n"
        "Not included: issues and pull-request discussions, Git LFS objects, separate "
        "submodule repositories, Go dependency sources, container images, VM configuration, "
        "secrets or chain databases. This snapshot preserves upstream material; "
        "it does not establish an offline rebuild.\n",
        encoding="utf-8",
    )


def write_checksums(destination):
    entries = []
    for path in sorted(destination.iterdir()):
        if not path.is_file() or path.stat().st_size >= RELEASE_FILE_LIMIT:
            raise ValueError(f"Snapshot file cannot be uploaded without splitting: {path.name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        entries.append(f"{digest}  {path.name}\n")
    if len(entries) >= 1000:
        raise ValueError("Snapshot exceeds GitHub's release asset count limit")
    (destination / "SHA256SUMS").write_text("".join(entries), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    collect_snapshot(arguments.destination.resolve(), datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
