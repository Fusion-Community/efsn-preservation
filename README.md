# efsn-preservation

Preservation copies of public Fusion source history, the upstream wiki, and upstream
release downloads. The maintained client remains in `Fusion-Community/efsn`.

## Run a snapshot

Open **Actions → Preserve upstream → Run workflow**, select **main**, and run it.
No personal token or server credentials need to be added. The workflow uses its own
repository-scoped token and publishes only in this repository.

Each run saves the advertised Git refs and reachable history of:

- `FUSIONFoundation/efsn`
- `Fusion-Community/efsn`
- `algorexxx/fsn-efsn`, which holds the deployed gateway integration
- `FUSIONFoundation/efsn.wiki.git`

Every bundle is cloned back locally, checked with `git fsck`, and compared with the
source refs. All upstream release pages and asset lists are paginated; each download
is checked against its advertised size and, when supplied, its upstream SHA-256.
Original names, URLs and release notes are retained as metadata. Asset files use
numeric IDs so upstream filenames cannot overwrite other snapshot files.

The workflow creates a uniquely named **draft** release, uploads the complete
snapshot, downloads it again and checks every checksum before publishing. A failed
upload/check leaves a draft, not a completed backup. Earlier releases are never
replaced or deleted. Rerunning creates a new snapshot. A missing source or asset fails
the run; a file of 2 GiB or more also fails until splitting is implemented.

## Restore

Download all assets from one published snapshot into an empty directory, then on Linux:

```bash
sha256sum --check SHA256SUMS
git clone upstream.bundle efsn-restored
git -C efsn-restored fsck --full
```

To restore every ref into a bare repository, use
`git clone --mirror upstream.bundle efsn-restored.git`. `manifest.json` records the
source HEAD ref and complete ref inventory. Other bundles restore in the same way.
For original release downloads, match the saved asset ID in `manifest.json` to
`upstream-releases.json` and restore its original filename.

Computed checksums verify faithful copies; they are not new upstream signatures or
an endorsement of the old binaries. Original licenses and notices remain applicable.

## Coverage

This first workflow covers Git history, the wiki and public upstream releases. It does
not archive issue/PR discussions, LFS objects, separate submodule repositories, Go
dependency sources or container images. Build dependencies and the exact deployed VM
image require the next preservation steps and an offline rebuild check. VM settings,
keys and chain databases do not belong in this public repository.

## Check changes

Python 3.11+ and Git are sufficient: `python3 -m unittest discover -v`.
Tests use temporary Git repositories and in-memory downloads; they do not publish
releases or contact the gateway.

## Preserve build inputs

After merging the build workflow, open **Actions → Preserve build inputs → Run
workflow** on **main**. This saves Linux/amd64 Docker archives in a separate dated
release. No container registry or personal token is needed.

The pinned Go 1.21.3 and Alpine base images, installed C build tools, complete Go
module cache (including source, ZIPs and licenses), dependency inventories, and
the preserved source bundles are included. The upstream baseline `c5f0174`,
community tracing fix `aae15bf`, and deployed gateway source `ac94b406` share the
same dependency files; the collector checks that they still match. The source
bundles come from the first community snapshot with fixed SHA-256 values.

A separate fresh runner loads only these archives and builds all three revisions
with container networking disabled, module downloads disabled, and empty Go build
caches. Both tracing fixes run their race regression tests; the gateway also runs
its bootstrap isolation test. Each resulting binary must report its expected
commit and Go version when executed with the saved Alpine runtime. PR checks run
this entire exercise without publishing. Only a manual main-branch run can publish,
after the uploaded copy passes checksum verification. Intermediate Actions artifacts
expire after one day; published release assets are the preservation copy.

For an offline Linux/amd64 Docker host with Python 3.11+ and Git, download all release
assets into an empty directory, then run:

```bash
sha256sum --check SHA256SUMS
python3 build_inputs.py verify . ../verification.json
```

Restore imports the saved Docker images and uses temporary containers with no
network and no node service. It retains the images for later use and writes a
verification report. Installed Alpine packages are inventoried at collection time;
they are not asserted to match the VM's original C toolchain bit for bit. This
checks offline rebuildability, not full-history consensus correctness. The exact
running VM image requires a separate export. Other architectures, older dependency
graphs, external test repositories, VM settings, secrets and chain data are excluded.
