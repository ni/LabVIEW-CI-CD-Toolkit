#!/usr/bin/env python3
"""Offline installer payload contracts for the opt-in Linux LVPM worker."""

from collections import defaultdict
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("lvpm_installer", ROOT / ".github/labview-ci/install.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)
HELPERS = {".github/labview/lvpm/worker.py", ".github/labview/lvpm/adapter.py"}
SHARED_HELPERS = HELPERS | {".github/labview/dragon_deps.py"}
CANDIDATE_FILES = {
    ".github/workflows/build-labview-lvpm-image.yml",
    ".github/docker/labview-ci-lvpm.Dockerfile",
    ".github/labview/vipm/ci-tooling.vipc",
    ".github/labview/toimages/",
}
LINUX_ACTIVITIES = ("masscompile", "vi-analyzer", "vidiff", "builds", "snapshots")
PLATFORMS = (("linux",), ("windows", "linux"), ("windows",))


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / ".github/labview-ci/catalog.json").read_text())

    def resolve(self, activities, platforms):
        with patch.object(INSTALLER, "warn"):
            return INSTALLER.resolve_file_list(self.catalog, list(activities), list(platforms))

    def stage(self, target, activities, platforms):
        entries = self.resolve(activities, platforms)
        with redirect_stdout(io.StringIO()):
            for entry in entries:
                self.assertTrue((ROOT / entry).exists(), entry)
                INSTALLER.copy_entry(entry, ROOT, target, [], True, False, defaultdict(int))
        return entries

    def test_helpers_available_without_custom_image_or_dashboard(self):
        for platforms in PLATFORMS:
            for activities in ((), ("dashboard",), *[(activity,) for activity in LINUX_ACTIVITIES]):
                with self.subTest(platforms=platforms, activities=activities):
                    entries = self.resolve(activities, platforms)
                    self.assertTrue(SHARED_HELPERS.issubset(entries))
                    self.assertNotIn(".github/workflows/build-labview-lvpm-image.yml", entries)

    def test_linux_custom_image_resolves_complete_candidate_payload(self):
        for platforms in PLATFORMS[:2]:
            for activities in (("custom-image",), ("dashboard", "custom-image")):
                with self.subTest(platforms=platforms, activities=activities):
                    self.assertTrue((SHARED_HELPERS | CANDIDATE_FILES).issubset(self.resolve(activities, platforms)))

    def test_candidate_files_registered_only_for_linux(self):
        capability = next(entry for entry in self.catalog["capabilities"] if entry["id"] == "custom-image")
        self.assertTrue(CANDIDATE_FILES.issubset(capability["files"]["linux"]))
        for group in (self.catalog["base"]["files"], capability["files"]["any"], capability["files"]["windows"]):
            self.assertNotIn(".github/workflows/build-labview-lvpm-image.yml", group)
            self.assertNotIn(".github/docker/labview-ci-lvpm.Dockerfile", group)

    def test_windows_keeps_existing_payload_without_candidate_build(self):
        previous = copy.deepcopy(self.catalog)
        previous["base"]["files"] = [entry for entry in previous["base"]["files"] if entry not in SHARED_HELPERS]
        for activities in (("custom-image",), ("dashboard", "custom-image", *LINUX_ACTIVITIES)):
            with self.subTest(activities=activities), patch.object(INSTALLER, "warn"):
                expected = INSTALLER.resolve_file_list(previous, list(activities), ["windows"])
                actual = self.resolve(activities, ["windows"])
                self.assertEqual([entry for entry in actual if entry not in SHARED_HELPERS], expected)
                self.assertNotIn(".github/workflows/build-labview-lvpm-image.yml", actual)
                self.assertNotIn(".github/docker/labview-ci-lvpm.Dockerfile", actual)

    def test_helper_registration_excludes_recursive_cache_or_test_payloads(self):
        self.assertTrue(SHARED_HELPERS.issubset(self.catalog["base"]["files"]))
        entries = self.resolve(("dashboard", "custom-image", *LINUX_ACTIVITIES), ["windows", "linux"])
        self.assertEqual({entry for entry in entries if entry.startswith(".github/labview/lvpm/")}, HELPERS)
        self.assertNotIn(".github/workflows/lvpm-checks.yml", entries)
        self.assertFalse(any("test-lvpm" in entry or "__pycache__" in entry or entry.endswith(".pyc") for entry in entries))

    def test_staged_candidate_has_workflow_helper_and_docker_copy_closure(self):
        for platforms in PLATFORMS[:2]:
            with self.subTest(platforms=platforms), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary)
                self.stage(target, ("custom-image",), platforms)
                for relative in SHARED_HELPERS | CANDIDATE_FILES | {".github/actions/publish-gh-pages/action.yml"}:
                    self.assertTrue((target / relative).exists(), relative)
                dockerfile = target / ".github/docker/labview-ci-lvpm.Dockerfile"
                for line in dockerfile.read_text().replace("\\\n", " ").splitlines():
                    if not line.startswith("COPY "):
                        continue
                    arguments = shlex.split(line)
                    if any(arg.startswith("--from=") for arg in arguments):
                        continue
                    for source in arguments[1:-1]:
                        if source.startswith(".github/"):
                            self.assertTrue((target / source).exists(), source)
                runtime_files = {entry.relative_to(target).as_posix() for entry in (target / ".github/labview/lvpm").rglob("*") if entry.is_file()}
                self.assertEqual(runtime_files, HELPERS)

    def test_each_linux_activity_stages_helpers_without_custom_image(self):
        for activity in LINUX_ACTIVITIES:
            with self.subTest(activity=activity), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary)
                entries = self.stage(target, (activity,), ["linux"])
                for relative in HELPERS:
                    self.assertTrue((target / relative).is_file(), relative)
                workflows = [entry for entry in entries if entry.startswith(".github/workflows/")]
                self.assertTrue(any("lvpm/worker.py" in (target / entry).read_text() for entry in workflows))
                self.assertFalse((target / ".github/workflows/build-labview-lvpm-image.yml").exists())

    def test_dashboard_without_custom_image_stages_reconfigure_dependencies(self):
        for platforms in PLATFORMS:
            with self.subTest(platforms=platforms), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary)
                self.stage(target, ("dashboard",), platforms)
                self.assertTrue((target / ".github/workflows/reconfigure.yml").is_file())
                self.assertTrue(all((target / relative).is_file() for relative in SHARED_HELPERS))
                self.assertFalse((target / ".github/workflows/build-labview-lvpm-image.yml").exists())

    def test_installed_dragon_parser_reads_real_declarations_without_source_imports(self):
        for platforms in PLATFORMS:
            with self.subTest(platforms=platforms), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary)
                self.stage(target, ("dashboard",), platforms)
                declaration = target / "project.dragon"
                declaration.write_text('[vipm.dependencies]\noglib_file = "6.0.2.28"\n')
                spec = importlib.util.spec_from_file_location("installed_dragon", target / ".github/labview/dragon_deps.py")
                parser = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(parser)
                self.assertTrue(parser.toml_available())
                inventory = parser.build_inventory(str(target))
                self.assertEqual(len(inventory["files"]), 1)
                self.assertEqual(inventory["install_set"][0]["package_id"], "oglib_file")
                self.assertEqual(inventory["install_set"][0]["declared_version"], "6.0.2.28")

    def test_base_catalog_preserves_qualified_container_metadata_without_release_workflows(self):
        for platforms in PLATFORMS:
            with self.subTest(platforms=platforms), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary)
                self.stage(target, (), platforms)
                installed = json.loads((target / ".github/labview-ci/catalog.json").read_text())
                self.assertEqual(installed["containers"], self.catalog["containers"])
                containers = installed["containers"]
                selected = next(entry for entry in containers["supported"] if entry["id"] == containers["default"])
                self.assertEqual(selected["status"], "qualified")
                self.assertRegex(selected["linuxTag"], r"^nationalinstruments/labview:[a-z0-9.-]+-linux$")
                self.assertNotIn(":latest", selected["linuxTag"])
                for workflow in ("release.yml", "promote-release.yml", "lvpm-checks.yml"):
                    self.assertFalse((target / ".github/workflows" / workflow).exists(), workflow)


if __name__ == "__main__":
    unittest.main()
