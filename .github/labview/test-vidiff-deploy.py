#!/usr/bin/env python3

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / ".github/labview/validate-vidiff-deploy-metadata.py"
EXTRACTOR = ROOT / ".github/labview/extract-vidiff-artifact.py"


class ValidateVidiffDeployMetadataTests(unittest.TestCase):
    def run_validator(self, metadata, *, env=None, capture_output_vars=False):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vidiff-meta.json"
            output_path = Path(temporary) / "github_output.txt"
            path.write_text(json.dumps(metadata), encoding="utf-8")
            environment = {
                **os.environ,
                "TRUSTED_ARTIFACT_NAME": f"vidiff-windows-{'a' * 40}",
                "TRUSTED_HEAD_SHA": "a" * 40,
                "TRUSTED_PR_NUMBER": "12",
                "TRUSTED_PLATFORM": "windows",
                "TRUSTED_RUN_CONCLUSION": "success",
            }
            if capture_output_vars:
                environment["GITHUB_OUTPUT"] = str(output_path)
            if env:
                environment.update(env)
            result = subprocess.run(
                [sys.executable, str(VALIDATOR), str(path)],
                capture_output=True,
                text=True,
                env=environment,
            )
            if not capture_output_vars:
                return result
            values = {}
            if output_path.exists():
                for line in output_path.read_text(encoding="utf-8").splitlines():
                    if "=" in line:
                        key, value = line.split("=", 1)
                        values[key] = value
            return result, values

    def test_accepts_matching_trusted_metadata(self):
        result = self.run_validator(
            {
                "head_sha": "a" * 40,
                "pr_number": "12",
                "platform": "windows",
                "outcome": "success",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_hostile_head_sha(self):
        result = self.run_validator(
            {
                "head_sha": "a" * 39 + "\n",
                "pr_number": "12",
                "platform": "windows",
                "outcome": "success",
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid metadata.head_sha", result.stderr)

    def test_rejects_provenance_mismatch(self):
        result = self.run_validator(
            {
                "head_sha": "a" * 40,
                "pr_number": "12",
                "platform": "linux",
                "outcome": "success",
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("platform mismatch", result.stderr)

    def test_rejects_non_positive_pr(self):
        result = self.run_validator(
            {
                "head_sha": "a" * 40,
                "pr_number": "0",
                "platform": "windows",
                "outcome": "success",
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid metadata.pr_number", result.stderr)

    def test_rejects_artifact_name_that_does_not_match_trusted_lineage(self):
        result = self.run_validator(
            {
                "head_sha": "a" * 40,
                "pr_number": "12",
                "platform": "windows",
                "outcome": "success",
            },
            env={"TRUSTED_ARTIFACT_NAME": "vidiff-linux-" + "a" * 40},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Artifact name mismatch", result.stderr)

    def test_preserves_metadata_failure_when_run_conclusion_is_success(self):
        result, outputs = self.run_validator(
            {
                "head_sha": "a" * 40,
                "pr_number": "12",
                "platform": "windows",
                "outcome": "failure",
            },
            capture_output_vars=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("metadata_outcome"), "failure")
        self.assertEqual(outputs.get("outcome"), "failure")

    def test_forces_failure_when_run_conclusion_is_non_success(self):
        result, outputs = self.run_validator(
            {
                "head_sha": "a" * 40,
                "pr_number": "12",
                "platform": "windows",
                "outcome": "success",
            },
            env={"TRUSTED_RUN_CONCLUSION": "cancelled"},
            capture_output_vars=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("metadata_outcome"), "success")
        self.assertEqual(outputs.get("outcome"), "failure")


class ExtractVidiffArtifactTests(unittest.TestCase):
    def run_extractor(self, entries):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        archive = root / "artifact.zip"
        destination = root / "report"
        with zipfile.ZipFile(archive, "w") as zipped:
            for entry in entries:
                if isinstance(entry, tuple):
                    name, content = entry
                    zipped.writestr(name, content)
                    continue
                info = zipfile.ZipInfo(entry["name"])
                info.create_system = 3
                info.external_attr = entry.get("external_attr", 0)
                zipped.writestr(info, entry.get("content", ""))
        result = subprocess.run(
            [sys.executable, str(EXTRACTOR), str(archive), str(destination)],
            capture_output=True,
            text=True,
        )
        return result, root, destination

    def test_extracts_normal_nested_paths_and_spaces(self):
        result, _, destination = self.run_extractor(
            [
                ("vidiff-meta.json", '{"head_sha":"a"}'),
                ("vidiff/changes with spaces.json", "[]"),
                ("vidiff/nested/path/report.html", "ok"),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((destination / "vidiff-meta.json").is_file())
        self.assertTrue((destination / "vidiff/changes with spaces.json").is_file())
        self.assertTrue((destination / "vidiff/nested/path/report.html").is_file())

    def test_rejects_parent_traversal_entries(self):
        result, root, destination = self.run_extractor(
            [
                ("vidiff/ok.txt", "ok"),
                ("../escape.txt", "x"),
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("parent traversal", result.stderr)
        self.assertFalse((root / "escape.txt").exists())
        self.assertFalse((destination / "escape.txt").exists())

    def test_rejects_absolute_or_drive_absolute_entries(self):
        for name, expected in [
            ("/abs.txt", "absolute path"),
            ("C:/abs.txt", "drive-absolute"),
        ]:
            with self.subTest(name=name):
                result, _, destination = self.run_extractor([(name, "x")])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertFalse((destination / "abs.txt").exists())

    def test_rejects_symlink_entries(self):
        symlink_mode = 0o120777
        result, _, destination = self.run_extractor(
            [
                {"name": "vidiff/link", "external_attr": symlink_mode << 16, "content": "target"},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertFalse((destination / "vidiff/link").exists())


if __name__ == "__main__":
    unittest.main()
