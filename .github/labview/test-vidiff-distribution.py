#!/usr/bin/env python3
"""Check that VIDiff installs its deployment security helpers on both platforms."""

from collections import defaultdict
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("vidiff_installer", ROOT / ".github/labview-ci/install.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)
HELPERS = {
    ".github/labview/extract-vidiff-artifact.py",
    ".github/labview/validate-vidiff-deploy-metadata.py",
}
PLATFORMS = (("windows",), ("linux",), ("windows", "linux"))


class VIDiffDistributionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / ".github/labview-ci/catalog.json").read_text())

    def test_helpers_resolve_with_deployment_workflow_for_each_platform(self):
        for platforms in PLATFORMS:
            with self.subTest(platforms=platforms):
                entries = INSTALLER.resolve_file_list(self.catalog, ["vidiff"], list(platforms))
                self.assertIn(".github/workflows/vidiff-deploy.yml", entries)
                self.assertTrue(HELPERS.issubset(entries))
                self.assertEqual(len(entries), len(set(entries)))

    def test_helpers_are_not_installed_without_vidiff(self):
        for platforms in PLATFORMS:
            with self.subTest(platforms=platforms):
                entries = INSTALLER.resolve_file_list(self.catalog, [], list(platforms))
                self.assertFalse(HELPERS.intersection(entries))
                self.assertNotIn(".github/workflows/vidiff-deploy.yml", entries)

    def test_scratch_client_contains_exact_helpers_and_not_security_tests(self):
        for platforms in PLATFORMS:
            with self.subTest(platforms=platforms), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary)
                entries = INSTALLER.resolve_file_list(self.catalog, ["vidiff"], list(platforms))
                with redirect_stdout(io.StringIO()):
                    for entry in entries:
                        INSTALLER.copy_entry(entry, ROOT, target, [], True, False, defaultdict(int))
                workflow = (target / ".github/workflows/vidiff-deploy.yml").read_text()
                for relative in HELPERS:
                    installed = target / relative
                    self.assertTrue(installed.is_file(), relative)
                    self.assertEqual(installed.read_bytes(), (ROOT / relative).read_bytes())
                    self.assertIn(relative, workflow)
                for relative in (
                    ".github/workflows/vidiff-deploy-checks.yml",
                    ".github/labview/test-vidiff-deploy.py",
                    ".github/labview/test-vidiff-deploy.mjs",
                    ".github/labview/test-vidiff-distribution.py",
                ):
                    self.assertFalse((target / relative).exists(), relative)


if __name__ == "__main__":
    unittest.main()
