import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_inputs


class BuildInputTests(unittest.TestCase):
    def make_snapshot(self, directory):
        snapshot = Path(directory)
        (snapshot / "asset.bin").write_bytes(b"abc")
        (snapshot / "SHA256SUMS").write_text(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  asset.bin\n",
            encoding="utf-8",
        )
        return snapshot

    def test_restore_accepts_known_checksum_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(directory)

            build_inputs.verify_checksums(snapshot)

    def test_restore_rejects_changed_payload_before_docker_load(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(directory)
            (snapshot / "asset.bin").write_bytes(b"abd")
            with patch.object(build_inputs, "run") as runner:

                with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                    build_inputs.verify_inputs(snapshot, snapshot / "report.json")

                runner.assert_not_called()

    def test_restore_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(directory)
            (snapshot / "asset.bin").unlink()

            with self.assertRaisesRegex(ValueError, "inventory"):
                build_inputs.verify_checksums(snapshot)

    def test_restore_rejects_unlisted_file(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(directory)
            (snapshot / "extra.bin").write_bytes(b"extra")

            with self.assertRaisesRegex(ValueError, "inventory"):
                build_inputs.verify_checksums(snapshot)

    def test_restore_rejects_duplicate_checksum_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(directory)
            checksums = snapshot / "SHA256SUMS"
            checksums.write_text(checksums.read_text(encoding="utf-8") * 2, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate"):
                build_inputs.verify_checksums(snapshot)

    def test_restore_rejects_checksum_path_outside_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(directory)
            checksums = snapshot / "SHA256SUMS"
            checksums.write_text(checksums.read_text(encoding="utf-8").replace("asset.bin", "../asset.bin"), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid"):
                build_inputs.verify_checksums(snapshot)

    def test_collection_refuses_existing_snapshot_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(directory)

            with self.assertRaises(FileExistsError):
                build_inputs.collect_inputs(snapshot)

            self.assertEqual(b"abc", (snapshot / "asset.bin").read_bytes())


if __name__ == "__main__":
    unittest.main()
