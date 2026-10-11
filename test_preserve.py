import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import preserve


class PreservationTests(unittest.TestCase):
    def test_bundle_restores_branches_annotated_tag_and_old_content(self):
        with tempfile.TemporaryDirectory(prefix="efsn-test-") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            output = root / "snapshot"
            output.mkdir()
            self.run_git(source, "init", "--quiet", "--initial-branch=main")
            self.commit_text(source, "original\n")
            self.run_git(source, "tag", "-a", "v1", "-m", "Original release")
            self.run_git(source, "checkout", "--quiet", "-b", "feature/example")
            self.commit_text(source, "feature\n")
            self.run_git(source, "checkout", "--quiet", "main")
            self.commit_text(source, "main\n")

            result = preserve.preserve_repository("example", str(source), output)
            restored = root / "restored.git"
            self.run_git(root, "clone", "--quiet", "--mirror", str(output / "example.bundle"), str(restored))
            main_text = self.run_git(restored, "show", "refs/heads/main:file.txt")
            feature_text = self.run_git(restored, "show", "refs/heads/feature/example:file.txt")
            release_text = self.run_git(restored, "show", "refs/tags/v1:file.txt")

            self.assertEqual("refs/heads/main", result["head_ref"])
            self.assertEqual({"refs/heads/main", "refs/heads/feature/example", "refs/tags/v1"}, set(result["refs"]))
            self.assertEqual("main\n", main_text)
            self.assertEqual("feature\n", feature_text)
            self.assertEqual("original\n", release_text)

    def commit_text(self, source, content):
        (source / "file.txt").write_text(content, encoding="utf-8")
        self.run_git(source, "add", "file.txt")
        self.run_git(source, "commit", "--quiet", "-m", content.strip())

    def run_git(self, directory, *arguments):
        return subprocess.run(
            ["git", "-c", "user.name=Preservation test", "-c", "user.email=test@example.invalid",
             "-c", "core.hooksPath=/dev/null", *arguments],
            cwd=directory, check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        ).stdout

    def test_copy_asset_accepts_known_sha256_vector(self):
        source = io.BytesIO(b"abc")
        target = io.BytesIO()
        digest = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

        preserve.copy_asset(source, target, 3, digest)

        self.assertEqual(b"abc", target.getvalue())

    def test_copy_asset_rejects_truncated_download(self):
        source = io.BytesIO(b"ab")
        target = io.BytesIO()

        with self.assertRaisesRegex(ValueError, "truncated"):
            preserve.copy_asset(source, target, 3, None)

    def test_copy_asset_rejects_oversized_download(self):
        source = io.BytesIO(b"abcd")
        target = io.BytesIO()

        with self.assertRaisesRegex(ValueError, "exceeds"):
            preserve.copy_asset(source, target, 3, None)

    def test_copy_asset_rejects_digest_mismatch(self):
        source = io.BytesIO(b"abc")
        target = io.BytesIO()

        with self.assertRaisesRegex(ValueError, "digest"):
            preserve.copy_asset(source, target, 3, "sha256:" + "0" * 64)

    def test_pagination_reads_past_first_hundred(self):
        first_page = list(range(100))
        last_page = ["final release"]
        endpoint = "/repos/FUSIONFoundation/efsn/releases"
        with patch.object(preserve, "read_github_json", side_effect=[first_page, last_page]) as reader:

            actual = preserve.read_pages(endpoint)

            self.assertEqual([*first_page, "final release"], actual)
            self.assertEqual([
                call(endpoint + "?per_page=100&page=1"),
                call(endpoint + "?per_page=100&page=2"),
            ], reader.call_args_list)

    def test_asset_download_does_not_send_api_token(self):
        asset = {
            "browser_download_url": "https://github.com/FUSIONFoundation/efsn/releases/download/v1/binary",
            "state": "uploaded", "size": 3,
        }
        with tempfile.TemporaryDirectory(prefix="efsn-test-") as directory:
            target = Path(directory) / "asset.bin"
            with patch.dict(os.environ, {"GH_TOKEN": "not-a-real-token"}):
                with patch.object(preserve.urllib.request, "urlopen", return_value=io.BytesIO(b"abc")) as opener:

                    preserve.download_asset(asset, target, "FUSIONFoundation/efsn")

                    self.assertIsNone(opener.call_args.args[0].get_header("Authorization"))
                    self.assertEqual(b"abc", target.read_bytes())

    def test_asset_download_rejects_unexpected_host(self):
        asset = {"browser_download_url": "https://example.invalid/FUSIONFoundation/efsn/releases/download/v1/binary"}

        with self.assertRaisesRegex(ValueError, "expected public GitHub repository"):
            preserve.download_asset(asset, Path("must-not-be-created.bin"), "FUSIONFoundation/efsn")

    def test_asset_download_rejects_a_different_release_repository(self):
        asset = {
            "browser_download_url": "https://github.com/FUSIONFoundation/efsn/releases/download/v1/binary",
        }

        with self.assertRaisesRegex(ValueError, "expected public GitHub repository"):
            preserve.download_asset(asset, Path("must-not-be-created.bin"), "FUSIONFoundation/myfusionwallet")

    def test_release_names_cannot_overwrite_files(self):
        releases = [{"id": 7, "assets": []}, {"id": 8, "assets": []}]
        assets = [
            [{"id": 21, "name": "../../manifest.json"}],
            [{"id": 22, "name": "../../manifest.json"}],
        ]
        with tempfile.TemporaryDirectory(prefix="efsn-test-") as directory:
            destination = Path(directory)
            with patch.object(preserve, "read_pages", side_effect=[releases, *assets]):
                with patch.object(preserve, "download_asset") as downloader:

                    result = preserve.preserve_releases(destination, "upstream", "FUSIONFoundation/efsn")

                    self.assertEqual([
                        {"release_id": 7, "asset_id": 21, "file": "upstream-release-7-asset-21.bin"},
                        {"release_id": 8, "asset_id": 22, "file": "upstream-release-8-asset-22.bin"},
                    ], result["assets"])
                    self.assertEqual([
                        call(assets[0][0], destination / "upstream-release-7-asset-21.bin", "FUSIONFoundation/efsn"),
                        call(assets[1][0], destination / "upstream-release-8-asset-22.bin", "FUSIONFoundation/efsn"),
                    ], downloader.call_args_list)

    def test_release_notes_without_assets_are_preserved_for_each_repository(self):
        wallet = [{"id": 7, "body": "Wallet release notes\n", "assets": []}]
        library = [{"id": 8, "body": "Library release notes\n", "assets": []}]
        with tempfile.TemporaryDirectory(prefix="efsn-test-") as directory:
            destination = Path(directory)
            with patch.object(preserve, "read_pages", side_effect=[wallet, [], library, []]):

                wallet_result = preserve.preserve_releases(destination, "myfusionwallet", "FUSIONFoundation/myfusionwallet")
                library_result = preserve.preserve_releases(destination, "web3-fusion-extend", "FUSIONFoundation/web3-fusion-extend")

                self.assertEqual({
                    "repository": "FUSIONFoundation/myfusionwallet",
                    "metadata": "myfusionwallet-releases.json", "assets": [],
                }, wallet_result)
                self.assertEqual({
                    "repository": "FUSIONFoundation/web3-fusion-extend",
                    "metadata": "web3-fusion-extend-releases.json", "assets": [],
                }, library_result)
                self.assertEqual(wallet, json.loads((destination / "myfusionwallet-releases.json").read_text(encoding="utf-8")))
                self.assertEqual(library, json.loads((destination / "web3-fusion-extend-releases.json").read_text(encoding="utf-8")))

    def test_snapshot_indexes_and_checksums_all_release_metadata(self):
        releases = [{"id": 7, "body": "Preserved notes", "assets": []}]
        expected_releases = [{
            "repository": "FUSIONFoundation/myfusionwallet",
            "metadata": "myfusionwallet-releases.json", "assets": [],
        }]
        with tempfile.TemporaryDirectory(prefix="efsn-test-") as directory:
            destination = Path(directory) / "snapshot"
            with patch.object(preserve, "REPOSITORIES", {}), patch.object(
                preserve, "RELEASE_REPOSITORIES", {"myfusionwallet": "FUSIONFoundation/myfusionwallet"}
            ), patch.object(preserve, "read_pages", side_effect=[releases, []]):

                preserve.collect_snapshot(destination, "2026-10-11T00:00:00+00:00")
                manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
                checksums = (destination / "SHA256SUMS").read_text(encoding="utf-8")

                self.assertEqual(expected_releases, manifest["releases"])
                self.assertIn("  myfusionwallet-releases.json\n", checksums)
                self.assertIn("  manifest.json\n", checksums)

    def test_snapshot_refuses_existing_directory_without_changing_it(self):
        with tempfile.TemporaryDirectory(prefix="efsn-test-") as directory:
            destination = Path(directory)
            sentinel = destination / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                preserve.collect_snapshot(destination, "2026-10-11T00:00:00+00:00")

            self.assertEqual([sentinel], list(destination.iterdir()))
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    def test_checksums_cover_saved_metadata_and_payload(self):
        with tempfile.TemporaryDirectory(prefix="efsn-test-") as directory:
            destination = Path(directory)
            (destination / "asset.bin").write_bytes(b"abc")
            (destination / "manifest.json").write_bytes(b"{}")

            preserve.write_checksums(destination)
            actual = (destination / "SHA256SUMS").read_text(encoding="utf-8")

            self.assertEqual(
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  asset.bin\n"
                "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a  manifest.json\n",
                actual,
            )


if __name__ == "__main__":
    unittest.main()
