import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from preserve import copy_asset, run_git, write_checksums, write_json


ROOT = Path(__file__).resolve().parent
SOURCE_SNAPSHOT = "https://github.com/Fusion-Community/efsn-preservation/releases/download/snapshot-20261011T131057Z-38142118280-1"
BUNDLES = {
    "community.bundle": {
        "size": 21588581,
        "sha256": "ad833b305d99f3cf77f63d9e2120de2f7f01d22e42d533b47fbb1428778fcdd6",
    },
    "gateway.bundle": {
        "size": 40746912,
        "sha256": "e0db650abc6b30d57c6bfe5ef1793eae4fe3f55c0c1e933545a18181c0ad1246",
    },
}
SOURCES = {
    "upstream": {"bundle": "community.bundle", "commit": "c5f0174d88ab2b9c3086c6a9cc9ccf38a072992f"},
    "community": {"bundle": "community.bundle", "commit": "aae15bf0e9d5ac16b5d1fcb5e508adf4c9ad32f4"},
    "gateway": {"bundle": "gateway.bundle", "commit": "ac94b40659a211f43db66963507c227395628a8e"},
}
BASE_IMAGES = {
    "builder": "golang@sha256:96a8a701943e7eabd81ebd0963540ad660e29c3b2dc7fb9d7e06af34409e9ba6",
    "runtime": "alpine@sha256:de0eb0b3f2a47ba1eb89389859a9bd88b28e82f5826b6969ad604979713c2d4f",
}


def collect_inputs(destination):
    destination.mkdir()
    for name, expected in BUNDLES.items():
        print(f"Downloading preserved source {name}", flush=True)
        request = urllib.request.Request(f"{SOURCE_SNAPSHOT}/{name}", headers={"User-Agent": "efsn-preservation"})
        with urllib.request.urlopen(request, timeout=120) as response, (destination / name).open("xb") as target:
            copy_asset(response, target, expected["size"], "sha256:" + expected["sha256"])
    with tempfile.TemporaryDirectory(prefix="efsn-build-inputs-") as directory:
        work = Path(directory)
        sources = restore_sources(destination, work)
        context = work / "context"
        context.mkdir()
        for name in ("go.mod", "go.sum"):
            shutil.copyfile(sources["gateway"] / name, context / name)
        images = {}
        for role, reference in BASE_IMAGES.items():
            run("docker", "pull", "--platform=linux/amd64", reference)
            images[role] = inspect_image(reference)
            images[role]["source"] = reference
        image_file = work / "prepared-image-id"
        run("docker", "build", "--platform=linux/amd64", "--no-cache", "--iidfile", str(image_file),
            "--build-arg", "BUILDER_IMAGE=" + BASE_IMAGES["builder"],
            "--file", str(ROOT / "Dockerfile.build-inputs"), str(context))
        images["prepared"] = inspect_image(image_file.read_text(encoding="utf-8").strip())
        copy_dependency_metadata(images["prepared"]["id"], destination)
        save_images(images, destination / "images.tar.gz")
    for name in ("build_inputs.py", "preserve.py", "offline-build.sh", "Dockerfile.build-inputs"):
        shutil.copyfile(ROOT / name, destination / name)
    write_json(destination / "build-manifest.json", {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "workflow_commit": os.environ.get("GITHUB_SHA"),
        "platform": "linux/amd64", "go_version": "go1.21.3",
        "source_snapshot": SOURCE_SNAPSHOT, "sources": SOURCES, "bundles": BUNDLES, "images": images,
    })
    (destination / "SNAPSHOT.md").write_text(
        "# Build input preservation\n\n"
        "Linux/amd64, Go 1.21.3. Source revisions and Docker image IDs are in build-manifest.json. "
        "images.tar.gz contains the pinned Go and Alpine base images plus a prepared compiler image "
        "with the C tools and complete Go module cache. Module source, ZIP files, licenses, hashes "
        "and package inventories are retained. The prepared image captures Alpine packages available "
        "at collection time; it does not claim the exact C package versions used on the VM.\n\n"
        "Published releases have passed a separate fresh-runner restore: Docker archives loaded, "
        "all three clients built with networking disabled and empty compilation caches, tracing "
        "regressions run with the race detector for the community/gateway revisions, and binaries "
        "executed with the saved Alpine runtime for a version check. See verification.json. "
        "This checks rebuildability, not bit-for-bit reproduction of the deployed binary or "
        "full-history consensus qualification.\n\n"
        "To repeat on an offline Linux/amd64 Docker host with Python 3.11+ and Git, download all "
        "release assets into an empty directory, verify SHA256SUMS, then run "
        "`python3 build_inputs.py verify . ../verification.json`. It creates only temporary test "
        "containers and imports the saved images; no node service is started.\n\n"
        "The exact deployed VM image still needs a separate export. Other architectures, older "
        "dependency graphs, external test repositories, VM settings, secrets and chain databases "
        "are outside this snapshot. Existing licenses and notices remain applicable.\n",
        encoding="utf-8",
    )
    write_checksums(destination)


def restore_sources(snapshot, work):
    sources = {}
    expected_dependencies = None
    for variant, entry in SOURCES.items():
        source = work / variant
        run_git("clone", "--quiet", "--no-checkout", str(snapshot / entry["bundle"]), str(source))
        run_git("checkout", "--quiet", "--detach", entry["commit"], cwd=source)
        run_git("fsck", "--full", "--no-dangling", cwd=source)
        dependencies = [(source / name).read_bytes() for name in ("go.mod", "go.sum")]
        if expected_dependencies is not None and dependencies != expected_dependencies:
            raise ValueError("Source dependency files differ; shared cache must be reconsidered")
        expected_dependencies = dependencies
        sources[variant] = source
    return sources


def run(*arguments, **kwargs):
    subprocess.run(arguments, check=True, **kwargs)


def capture(*arguments):
    return subprocess.run(arguments, check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8").stdout


def inspect_image(reference):
    result = json.loads(capture("docker", "image", "inspect", reference))[0]
    if result["Os"] != "linux" or result["Architecture"] != "amd64":
        raise ValueError("Unexpected image platform")
    return {"id": result["Id"], "os": result["Os"], "architecture": result["Architecture"]}


def copy_dependency_metadata(image_id, destination):
    container = capture("docker", "create", "--network=none", "--entrypoint=/bin/true", image_id).strip()
    try:
        for source, name in {
            "downloads.json": "module-downloads.json", "modules.json": "modules.json",
            "go.sum": "resolved-go.sum", "apk-packages.txt": "apk-packages.txt",
        }.items():
            run("docker", "cp", f"{container}:/dependencies/{source}", str(destination / name))
    finally:
        run("docker", "rm", container)


def save_images(images, destination):
    with subprocess.Popen(["docker", "image", "save", *[entry["id"] for entry in images.values()]],
                          stdout=subprocess.PIPE) as process:
        with gzip.open(destination, "xb", compresslevel=6) as target:
            shutil.copyfileobj(process.stdout, target)
        if process.wait() != 0:
            raise RuntimeError("Docker image export failed")


def verify_checksums(snapshot):
    entries = {}
    for line in (snapshot / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if not match or match[2] in entries or match[2] == "SHA256SUMS":
            raise ValueError("Invalid or duplicate checksum entry")
        entries[match[2]] = match[1]
    actual_names = {path.name for path in snapshot.iterdir() if path.name != "SHA256SUMS" and path.name != "__pycache__"}
    if not entries or actual_names != set(entries):
        raise ValueError("Checksum inventory differs from snapshot files")
    for name, digest in entries.items():
        path = snapshot / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Checksum target is not a regular file")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError(f"Checksum mismatch: {name}")


def verify_inputs(snapshot, report):
    if report.exists():
        raise FileExistsError(report)
    verify_checksums(snapshot)
    manifest = json.loads((snapshot / "build-manifest.json").read_text(encoding="utf-8"))
    if manifest["sources"] != SOURCES or manifest["bundles"] != BUNDLES:
        raise ValueError("Unexpected source manifest")
    run("docker", "image", "load", "--input", str(snapshot / "images.tar.gz"))
    images = manifest["images"]
    for entry in images.values():
        actual = inspect_image(entry["id"])
        if any(actual[key] != entry[key] for key in actual):
            raise ValueError("Restored image identity differs")
    results = {}
    with tempfile.TemporaryDirectory(prefix="efsn-offline-restore-") as directory:
        work = Path(directory)
        sources = restore_sources(snapshot, work)
        for variant, source in sources.items():
            output = work / f"{variant}-output"
            output.mkdir()
            entry = SOURCES[variant]
            print(f"Offline build and checks: {variant} {entry['commit']}", flush=True)
            run("docker", "run", "--rm", "--pull=never", "--network=none", "--cpus=2", "--memory=6g",
                "--env=GOPROXY=off", "--env=GOSUMDB=off", "--env=GOTOOLCHAIN=local",
                "--env=GOFLAGS=-mod=readonly -buildvcs=false", "--env=GOCACHE=/tmp/fresh-go-build",
                "--env=GOMAXPROCS=2",
                "--volume", f"{source}:/source:ro", "--volume", f"{output}:/output",
                "--volume", f"{snapshot / 'offline-build.sh'}:/offline-build.sh:ro",
                "--entrypoint=/bin/sh", images["prepared"]["id"], "/offline-build.sh", entry["commit"], variant)
            version = capture("docker", "run", "--rm", "--pull=never", "--network=none",
                              "--volume", f"{output}:/candidate:ro", "--entrypoint=/candidate/efsn",
                              images["runtime"]["id"], "version")
            if "Go Version: go1.21.3" not in version or entry["commit"] not in version:
                raise ValueError("Rebuilt binary reports an unexpected Go version or source commit")
            with (output / "efsn").open("rb") as stream:
                binary_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            results[variant] = {"commit": entry["commit"], "version": version, "binary_sha256": binary_hash,
                                "tracing_race_tests": variant != "upstream"}
    write_json(report, {"verified_at": datetime.now(timezone.utc).isoformat(),
                        "network": "none", "compilation_cache": "fresh per variant", "results": results})


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("destination", type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("snapshot", type=Path)
    verify.add_argument("report", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "collect":
        collect_inputs(arguments.destination.resolve())
    else:
        verify_inputs(arguments.snapshot.resolve(), arguments.report.resolve())


if __name__ == "__main__":
    main()
