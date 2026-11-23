#!/usr/bin/env python3
"""Offline regression tests for the toolkit's external LVPM adapter."""

import configparser
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parent.parent
SPEC = importlib.util.spec_from_file_location("lvpm_adapter", ROOT / "lvpm/adapter.py")
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)


class LvpmAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        home = patch.object(ADAPTER.Path, "home", return_value=self.root / "home")
        home.start()
        self.addCleanup(home.stop)
        self.write_vipc(ADAPTER.CORE, ["core_lib-1.0.0"])

    def write_vipc(self, relative, packages):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "<Configuration>" + "".join(
            f"<Package><Name>{name}</Name></Package>" for name in packages) + "</Configuration>"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("config.xml", content)
        return path

    def config(self, container):
        path = self.root / ".github/labview-ci.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("config:\n  labviewVersion: '2026'\n  container:\n" + container)

    def spec(self, os_gate="ALL", requires="", group_gate=None):
        text = ('[Package]\nName=some_lib\nVersion=1.0.0\n[Platform]\n'
                f'Exclusive_OS={os_gate}\n[Dependencies]\nRequires={requires}\n')
        if group_gate:
            text += f"[File Group 0]\nExclusive_OS={group_gate}\n"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("spec", text)
        return buffer.getvalue()

    def cached(self, data):
        cache = self.root / "cache"
        sha256 = ADAPTER.digest(data)
        blob = cache / "content/sha256" / sha256[:2] / sha256
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(data)
        with sqlite3.connect(cache / "lvpm-cache.db") as connection:
            connection.execute("CREATE TABLE packages (name TEXT, version TEXT, sha256 TEXT)")
            connection.execute("INSERT INTO packages VALUES (?, ?, ?)", ("some_lib", "1.0.0", sha256))
        connection.close()
        return cache

    def bundled(self, child_name="nested_lib", declaration="nested_lib-2.0.0", requires="nested_lib=2.0.0"):
        parent = self.spec(requires=requires)
        child_buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.spec())) as original, zipfile.ZipFile(child_buffer, "w") as archive:
            archive.writestr("spec", original.read("spec").replace(b"some_lib", child_name.encode()).replace(b"1.0.0", b"2.0.0"))
        child = child_buffer.getvalue()
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(parent)) as original, zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("spec", original.read("spec") + f"[Files]\nSub-Packages={declaration}\n".encode())
            archive.writestr("Packages/nested_lib-2.0.0.vip", child)
        return buffer.getvalue()

    def test_release_suffix_survives_translation(self):
        self.write_vipc("Dependencies.vipc", ["jki_rsc_toolkits_palette-1.1-1"])
        plan = ADAPTER.make_plan(self.root)
        package = next(package for package in plan["requirements"] if package["name"].startswith("jki_"))
        self.assertEqual(package["version"], "1.1-1")
        self.assertIn('"jki_rsc_toolkits_palette" = "1.1-1"', ADAPTER.manifest_text(plan["requirements"]))

    def test_conflicting_versions_fail_instead_of_first_wins(self):
        self.write_vipc("Dependencies.vipc", ["core_lib-2.0.0"])
        with self.assertRaisesRegex(ValueError, "Conflicting versions.*core_lib"):
            ADAPTER.make_plan(self.root)

    def test_repository_example_shared_vipc_pins_match_core_versions(self):
        core = REPOSITORY / ADAPTER.CORE
        example = REPOSITORY / "example/Dependencies.vipc"
        core_pins = {package["name"]: package["version"]
                     for package in ADAPTER.declarations(core, ADAPTER.CORE)}
        example_pins = {package["name"]: package["version"]
                        for package in ADAPTER.declarations(example, "example/Dependencies.vipc")}
        shared = sorted(set(core_pins) & set(example_pins))
        self.assertGreater(len(shared), 0)
        for name in shared:
            self.assertEqual(example_pins[name], core_pins[name],
                             f"example/Dependencies.vipc drifted from core for {name}")

    def test_repository_plan_keeps_shared_core_and_example_roots_exact(self):
        plan = ADAPTER.make_plan(REPOSITORY, labview_version="2026")
        requirements = {package["name"]: package for package in plan["requirements"]}
        expected = {
            "oglib_array": "6.0.1.20",
            "oglib_error": "6.0.0.26",
            "oglib_file": "6.0.2.28",
            "oglib_lvdata": "6.0.0.45",
            "oglib_string": "6.0.2.35",
        }
        for name, version in expected.items():
            package = requirements.get(name)
            self.assertIsNotNone(package)
            self.assertEqual(package["version"], version)
            self.assertIn(ADAPTER.CORE, package["source_files"])
            self.assertIn("example/Dependencies.vipc", package["source_files"])

    def test_repository_example_vipc_members_match_declared_packages(self):
        path = REPOSITORY / "example/Dependencies.vipc"
        expected = {
            "oglib_array-6.0.1.20",
            "oglib_error-6.0.0.26",
            "oglib_file-6.0.2.28",
            "oglib_lvdata-6.0.0.45",
            "oglib_string-6.0.2.35",
            "dataflow_g_lib_g_image-0.5.0.1",
            "oglib_appcontrol-4.1.0.7",
            "ni_lib_utf_junit_report-1.0.1.43",
            "ni_lib_junit_results_api-1.0.1.6",
            "ni_lib_simple_xml-1.0.0.4",
        }
        expected_members = {"config.xml"} | {
            f"{package}.{suffix}" for package in expected for suffix in ("spec", "bmp")}
        with zipfile.ZipFile(path) as archive, zipfile.ZipFile(REPOSITORY / ADAPTER.CORE) as core:
            self.assertCountEqual(archive.namelist(), expected_members)
            tree = ElementTree.fromstring(archive.read("config.xml"))
            declared = [(package.findtext("Name") or "").strip()
                        for package in tree.iter("Package")]
            self.assertCountEqual(declared, expected)
            for package in declared:
                with self.subTest(package=package):
                    spec = configparser.ConfigParser(interpolation=None, strict=False)
                    spec.read_string(archive.read(f"{package}.spec").decode("utf-8-sig"))
                    name = spec["Package"]["Name"].strip('"')
                    version = spec["Package"]["Version"].strip('"')
                    self.assertEqual(f"{name}-{version}", package)
                    for suffix in ("spec", "bmp"):
                        member = f"{package}.{suffix}"
                        if member in core.namelist():
                            self.assertEqual(archive.read(member), core.read(member))

    def test_same_version_retains_both_sources(self):
        self.write_vipc("Dependencies.vipc", ["core_lib-1.0.0"])
        self.assertEqual(len(ADAPTER.make_plan(self.root)["requirements"][0]["source_files"]), 2)

    def selected_repository_sources(self):
        sources = [
            ".github/labview/antidoc/antidoc.vipc",
            ".github/labview/experimental.vipc",
            ADAPTER.CORE,
            "example/Dependencies.vipc",
            "example/Example.dragon",
        ]
        for source in sources:
            target = self.root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY / source, target)
        self.config(
            "    vipc:\n" + "".join(f"      - path: {source}\n        monitor: true\n"
                                   for source in sources[:-1]) +
            f"    dragon:\n      - path: {sources[-1]}\n        monitor: true\n")
        return sources

    def test_actual_all_selected_repository_cli_retains_35_exact_roots_and_sources(self):
        sources = self.selected_repository_sources()
        output = self.root / "plan"
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "lvpm/adapter.py"), "plan",
             "--repo", str(self.root), "--out", str(output)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads((output / "requirements.json").read_text())
        self.assertEqual({entry["path"] for entry in plan["files"]}, set(sources))
        expected = {}
        for source in sources:
            for package in ADAPTER.declarations(REPOSITORY / source, source):
                if package["name"] in expected:
                    self.assertEqual(expected[package["name"]]["version"], package["version"])
                    expected[package["name"]]["source_files"].append(source)
                else:
                    expected[package["name"]] = package
        self.assertEqual(len(expected), 35)
        requirements = {package["name"]: package for package in plan["requirements"]}
        self.assertEqual(requirements.keys(), expected.keys())
        for name, package in expected.items():
            self.assertEqual(requirements[name]["version"], package["version"])
            self.assertCountEqual(requirements[name]["source_files"], package["source_files"])
        self.assertEqual(requirements["wovalab_lib_antidoc_cli"]["version"], "3.4.0.126")
        self.assertEqual(requirements["oglib_boolean"]["version"], "6.0.0.9")
        self.assertEqual(requirements["oglib_array"]["version"], "6.0.1.20")
        self.assertEqual((output / "lvpm.toml").read_text(),
                         ADAPTER.manifest_text(plan["requirements"]))

    def test_actual_all_selected_cli_rejects_divergent_experimental_pin_without_output(self):
        self.selected_repository_sources()
        path = self.root / ".github/labview/experimental.vipc"
        with zipfile.ZipFile(path) as archive:
            entries = [(entry, archive.read(entry.filename)) for entry in archive.infolist()]
        with zipfile.ZipFile(path, "w") as archive:
            for entry, content in entries:
                if entry.filename == "config.xml":
                    content = content.replace(b"3.4.0.126", b"3.3.0.117")
                archive.writestr(entry, content)
        output = self.root / "rejected"
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "lvpm/adapter.py"), "plan",
             "--repo", str(self.root), "--out", str(output)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Conflicting versions of wovalab_lib_antidoc_cli", result.stderr)
        self.assertIn("3.4.0.126", result.stderr)
        self.assertIn("3.3.0.117", result.stderr)
        self.assertFalse(output.exists())

    def test_experimental_vipc_preserves_config_and_authentic_antidoc_metadata(self):
        package = "wovalab_lib_antidoc_cli-3.4.0.126"
        with zipfile.ZipFile(REPOSITORY / ".github/labview/experimental.vipc") as archive, \
                zipfile.ZipFile(REPOSITORY / ".github/labview/antidoc/antidoc.vipc") as authoritative:
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
            self.assertCountEqual(archive.namelist(), ["config.xml", f"{package}.spec", f"{package}.bmp"])
            config = archive.read("config.xml")
            self.assertEqual(config.count(package.encode()), 1)
            original_config = config.replace(package.encode(), b"wovalab_lib_antidoc_cli-3.3.0.117")
            self.assertEqual(hashlib.sha256(original_config).hexdigest(),
                             "94042d01de16eca063b4d626b538568bac2e16dcdb09f5755dd19ba2aac2ecb8")
            for suffix in ("spec", "bmp"):
                name = f"{package}.{suffix}"
                self.assertEqual(archive.read(name), authoritative.read(name))
            spec = configparser.ConfigParser(interpolation=None, strict=False)
            spec.read_string(archive.read(f"{package}.spec").decode("utf-8-sig"))
            self.assertEqual(ADAPTER.archive_identity(spec), ("wovalab_lib_antidoc_cli", "3.4.0.126"))
            metadata = json.loads((REPOSITORY / ".github/labview/antidoc/antidoc.packages.json").read_text())
            cli = next(entry for entry in metadata["vipmPackages"]["packages"]
                       if entry["name"] == "wovalab_lib_antidoc_cli")
            self.assertEqual(cli["version"], "3.4.0.126")

    def test_lvpm_checks_cover_antidoc_and_experimental_declaration_changes(self):
        workflow = ADAPTER.yaml.load((REPOSITORY / ".github/workflows/lvpm-checks.yml").read_text(),
                                     Loader=ADAPTER.yaml.BaseLoader)
        for event in ("pull_request", "push"):
            for source in (".github/labview/experimental.vipc", ".github/labview/antidoc/antidoc.vipc",
                           ".github/labview/antidoc/antidoc.packages.json"):
                self.assertIn(source, workflow["on"][event]["paths"])

    def test_unchanged_pins_still_invalidate_fingerprint_when_declaration_bytes_change(self):
        path = self.write_vipc("Dependencies.vipc", ["other_lib-1.0.0"])
        original = ADAPTER.make_plan(self.root)
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("review.txt", "Declaration metadata changed without changing pins")
        changed = ADAPTER.make_plan(self.root)
        self.assertEqual(changed["requirements"], original["requirements"])
        self.assertNotEqual(changed["requirements_fingerprint"], original["requirements_fingerprint"])

    def test_monitor_selection_is_linux_specific(self):
        self.write_vipc("Windows.vipc", ["windows_lib-1.0.0"])
        self.write_vipc("Linux.vipc", ["linux_lib-1.0.0"])
        self.config('    vipc:\n      - path: Windows.vipc\n        monitor: true\n'
                    '        monitorOn: windows\n      - path: Linux.vipc\n        monitor: true\n'
                    '        monitorOn: [linux]\n')
        self.assertEqual([package["name"] for package in ADAPTER.make_plan(self.root)["requirements"]],
                         ["core_lib", "linux_lib"])

    def test_explicit_empty_selection_does_not_discover_project_files(self):
        self.write_vipc("Dependencies.vipc", ["not_selected-1.0.0"])
        self.config("    vipc: []\n    dragon: []\n")
        self.assertEqual(len(ADAPTER.make_plan(self.root)["requirements"]), 1)

    def test_dragon_inline_version_is_supported(self):
        (self.root / "Project.dragon").write_text('[vipm.dependencies]\nother_lib = { version = "2.0.0" }\n')
        self.assertEqual(ADAPTER.make_plan(self.root)["requirements"][1]["name"], "other_lib")

    def test_nipm_is_not_silently_discarded(self):
        (self.root / "Project.dragon").write_text('[nipm.dependencies]\nni_driver = "1.0.0"\n')
        with self.assertRaisesRegex(ValueError, "cannot install NIPM"):
            ADAPTER.make_plan(self.root)

    def test_dragon_embedded_vipc_is_rejected(self):
        (self.root / "Project.dragon").write_text('[project]\nvipc = "Other.vipc"\n')
        with self.assertRaisesRegex(ValueError, "Embedded Dragon"):
            ADAPTER.make_plan(self.root)

    def test_unpinned_versions_are_rejected(self):
        (self.root / "Project.dragon").write_text('[vipm.dependencies]\nother_lib = ">=2.0.0"\n')
        with self.assertRaisesRegex(ValueError, "exact package version"):
            ADAPTER.make_plan(self.root)

    def test_fingerprint_changes_with_requirements_not_activation(self):
        before = ADAPTER.make_plan(self.root)["requirements_fingerprint"]
        self.config("    linuxUse: linux-lvpm-abc@sha256:0123\n")
        self.assertEqual(ADAPTER.make_plan(self.root)["requirements_fingerprint"], before)
        self.write_vipc("Dependencies.vipc", ["other_lib-1.0.0"])
        self.assertNotEqual(ADAPTER.make_plan(self.root)["requirements_fingerprint"], before)

    def test_missing_monitored_file_fails(self):
        self.config("    vipc:\n      - path: Missing.vipc\n        monitor: true\n")
        with self.assertRaisesRegex(ValueError, "missing or outside"):
            ADAPTER.make_plan(self.root)

    def test_escaping_repository_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            ADAPTER.repository_path(self.root, "../escape.vipc")

    def test_windows_only_package_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "OS restriction"):
            ADAPTER.check_platform(ADAPTER.read_spec(self.spec(os_gate="Windows x64")))

    def test_windows_only_file_group_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "OS restriction"):
            ADAPTER.check_platform(ADAPTER.read_spec(self.spec(group_gate="Windows")))

    def test_linux_multi_platform_package_is_accepted(self):
        ADAPTER.check_platform(ADAPTER.read_spec(self.spec(os_gate="Windows x64,Mac OS>=10,Linux")))

    def test_labview_gate_is_enforced_even_for_exact_root_pins(self):
        spec = ADAPTER.read_spec(self.spec())
        spec["Platform"]["Exclusive_LabVIEW_Version"] = "LabVIEW>=27.0"
        with self.assertRaisesRegex(ValueError, "target LabVIEW is 26.3"):
            ADAPTER.check_labview(spec, "26.3")
        spec["Platform"]["Exclusive_LabVIEW_Version"] = ">=20.0"
        ADAPTER.check_labview(spec, "26.3")

    def test_missing_transitive_dependency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing transitive"):
            ADAPTER.check_requirements("missing_lib>=2.0.0", {})

    def test_conflicting_transitive_requirement_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Dependency conflict"):
            ADAPTER.check_requirements("other_lib>=2.0.0", {"other_lib": "1.0.0"})

    def test_exact_transitive_requirement_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "Dependency conflict"):
            ADAPTER.check_requirements("other_lib=2.0.0", {"other_lib": "3.0.0"})
        ADAPTER.check_requirements("other_lib>=1.1-1", {"other_lib": "1.1-2"})

    def test_dryrun_format_fails_closed(self):
        self.assertEqual(ADAPTER.resolved_packages("resolved 1 package:\n  other_lib 1.1-1\n\n"),
                         [{"name": "other_lib", "version": "1.1-1"}])
        with self.assertRaisesRegex(ValueError, "not recognized"):
            ADAPTER.resolved_packages("new output format")

    def test_discovery_only_defers_warnings_in_read_only_phase(self):
        for warning in ("dependency nested_lib unresolved, skipping", "listed in spec but absent from archive: legacy.vi"):
            with self.subTest(warning=warning), patch.object(ADAPTER.subprocess, "run", return_value=
                    subprocess.CompletedProcess(["lvpm"], 0, stdout=warning)):
                self.assertEqual(ADAPTER.run_lvpm(["install", "--dry-run"], {}, discovery=True), warning)
                with self.assertRaisesRegex(ValueError, "incomplete installation"):
                    ADAPTER.run_lvpm(["install", "--dry-run"], {})
        with self.assertRaisesRegex(ValueError, "must be read-only"):
            ADAPTER.run_lvpm(["install", "--hooks"], {}, discovery=True)
        for code, output in ((1, "unresolved"), (0, "pre-install FAILED")):
            with patch.object(ADAPTER.subprocess, "run", return_value=
                    subprocess.CompletedProcess(["lvpm"], code, stdout=output)):
                with self.assertRaisesRegex(ValueError, "incomplete installation"):
                    ADAPTER.run_lvpm(["install", "--dry-run"], {}, discovery=True)

    def test_embedded_package_closure_retains_original_and_install_hashes(self):
        data = self.bundled()
        destination = self.root / "packages"
        records = ADAPTER.cached_archives(self.cached(data), [{"name": "some_lib", "version": "1.0.0"}], destination)
        self.assertEqual({package["name"] for package in records}, {"some_lib", "nested_lib"})
        child = next(package for package in records if package["name"] == "nested_lib")
        self.assertEqual(child["embedded_source"], {"parent_sha256": ADAPTER.digest(data),
                                                  "archive_path": "Packages/nested_lib-2.0.0.vip"})
        for package in records:
            filename = f'{package["name"]}-{package["version"]}.vip'
            self.assertEqual(ADAPTER.digest((destination / filename).read_bytes()), package["install_sha256"])
            self.assertEqual(ADAPTER.digest((self.root / "source-packages" / filename).read_bytes()), package["sha256"])

    def test_missing_or_misidentified_embedded_package_fails(self):
        for data, message in ((self.bundled(declaration="missing_lib-2.0.0"), "Missing, ambiguous"),
                              (self.bundled(child_name="wrong_lib"), "identity does not match")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                with self.assertRaisesRegex(ValueError, message):
                    ADAPTER.cached_archives(self.cached(data), [{"name": "some_lib", "version": "1.0.0"}], self.root / "packages")

    def test_bundled_packages_do_not_mask_unresolved_or_conflicting_dependencies(self):
        for requirement in ("nested_lib=3.0.0", "missing_lib=1.0.0"):
            with self.subTest(requirement=requirement), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                with self.assertRaisesRegex(ValueError, "Dependency conflict|Missing transitive"):
                    ADAPTER.cached_archives(self.cached(self.bundled(requires=requirement)),
                                            [{"name": "some_lib", "version": "1.0.0"}], self.root / "packages")

    def test_zip_legacy_spec_names_are_losslessly_normalized(self):
        filename = "legacy H▄ll░.vi"
        text = f'[Package]\nName=some_lib\nVersion=1.0.0\n[File Group 0]\nFile 0="{filename}"\n'
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("spec", text.encode("cp437"))
            archive.writestr("File Group 0/" + filename, b"unchanged payload")
        normalized = ADAPTER.normalize_archive(buffer.getvalue())
        with zipfile.ZipFile(io.BytesIO(normalized)) as archive:
            self.assertEqual(archive.read("spec").decode("utf-8"), text)
            self.assertEqual(archive.read("File Group 0/" + filename), b"unchanged payload")
        self.assertEqual(normalized, ADAPTER.normalize_archive(buffer.getvalue()))

    def test_utf8_spec_and_payload_names_are_preserved(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("spec", '[Package]\nName=some_lib\nVersion=1.0.0\nDescription="café"\n')
            archive.writestr("File Group 0/café.vi", b"payload")
        with zipfile.ZipFile(io.BytesIO(ADAPTER.normalize_archive(buffer.getvalue()))) as archive:
            self.assertIn("café", archive.read("spec").decode())
            self.assertEqual(archive.read("File Group 0/café.vi"), b"payload")

    def test_inactive_legacy_labview_group_is_audited_not_installed(self):
        text = ('[Package]\nName=some_lib\nVersion=1.0.0\n[Platform]\nExclusive_LabVIEW_Version=>=6.1\n'
                '[Files]\nNum File Groups=2\n[File Group 0]\nExclusive_LabVIEW_Version="=6.1,=7.0,=7.1"\n'
                'File 0=old.vi\n[File Group 1]\nExclusive_LabVIEW_Version=>=8.0\nFile 0=current.vi\n')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("spec", text)
            archive.writestr("File Group 0/old.vi", b"old")
            archive.writestr("File Group 1/current.vi", b"current")
        data = buffer.getvalue()
        records = ADAPTER.cached_archives(self.cached(data), [{"name": "some_lib", "version": "1.0.0"}],
                                         self.root / "packages", "26.3")
        self.assertEqual(records[0]["inactive_file_groups"], {0: "=6.1,=7.0,=7.1"})
        with zipfile.ZipFile(self.root / "packages/some_lib-1.0.0.vip") as archive:
            self.assertEqual(set(archive.namelist()), {"spec", "File Group 0/current.vi"})
            metadata = ADAPTER.read_spec((self.root / "packages/some_lib-1.0.0.vip").read_bytes())
            self.assertEqual(metadata.get("File Group 0", "File 0"), "current.vi")
        with self.assertRaisesRegex(ValueError, "require a detected"):
            ADAPTER.normalize_archive(data)
        with self.assertRaisesRegex(ValueError, "Unsupported LabVIEW"):
            ADAPTER.labview_matches(">=8.0,unknown", "26.3")

    def test_unsafe_normalized_archive_is_rejected(self):
        for filename in ("../escape.vi", "/absolute.vi", "File Group 0/../../escape.vi"):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("spec", "[Package]\nName=some_lib\nVersion=1.0.0\n")
                archive.writestr(filename, b"payload")
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, "Unsafe"):
                ADAPTER.normalize_archive(buffer.getvalue())

    def test_frozen_sources_disable_network_indexes(self):
        manifest = ADAPTER.manifest_text([{"name": "other_lib", "version": "1.0.0"}], local=True)
        self.assertIn("defaults = false", manifest)
        self.assertNotIn("https://", manifest)

    def test_cached_archives_are_hashed_and_os_checked(self):
        cache = self.root / "cache"
        cache.mkdir()
        data = self.spec()
        sha256 = ADAPTER.digest(data)
        blob = cache / "content/sha256" / sha256[:2] / sha256
        blob.parent.mkdir(parents=True)
        blob.write_bytes(data)
        with sqlite3.connect(cache / "lvpm-cache.db") as connection:
            connection.execute("CREATE TABLE packages (name TEXT, version TEXT, sha256 TEXT)")
            connection.execute("INSERT INTO packages VALUES (?, ?, ?)", ("some_lib", "1.0.0", sha256))
        records = ADAPTER.cached_archives(cache, [{"name": "some_lib", "version": "1.0.0"}], self.root / "archives")
        self.assertEqual(records[0]["sha256"], sha256)
        blob.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Corrupt package cache"):
            ADAPTER.cached_archives(cache, [{"name": "some_lib", "version": "1.0.0"}], self.root / "archives")

    def test_installed_inventory_requires_files_hooks_and_exact_versions(self):
        installed = self.root / "installed"
        installed.mkdir()
        target = self.root / "library.vi"
        target.write_bytes(b"fixture")
        package = {"name": "some_lib", "version": "1.0.0", "files": [str(target)], "relinked": True,
               "skipped_hooks": [], "skipped_groups": []}
        record = installed / "some_lib.json"
        record.write_text(json.dumps(package))
        requirements = {"requirements": [{"name": "some_lib", "version": "1.0.0"}]}
        self.assertEqual(len(ADAPTER.verify_inventory(requirements, [package], installed)), 1)
        package["skipped_hooks"] = ["PostInstall"]
        record.write_text(json.dumps(package))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            ADAPTER.verify_inventory(requirements, [package], installed)
        package["skipped_hooks"] = []
        record.write_text(json.dumps(package))
        target.unlink()
        with self.assertRaisesRegex(ValueError, "files are missing"):
            ADAPTER.verify_inventory(requirements, [package], installed)

    def test_declared_install_hook_missing_from_archive_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.spec())) as original, zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("spec", original.read("spec") + b"\n[Script VIs]\nPostInstall=builder-hook.vi\n")
        data = buffer.getvalue()
        with self.assertRaisesRegex(ValueError, "declared install hook.*PostInstall"):
            ADAPTER.cached_archives(self.cached(data), [{"name": "some_lib", "version": "1.0.0"}],
                                    self.root / "archives")

    def test_inventory_rejects_wrong_target_duplicates_and_unknown_completion(self):
        installed = self.root / "installed"
        installed.mkdir()
        payload = self.root / "library.vi"
        payload.write_bytes(b"fixture")
        target = "LabVIEW 2026 (64-bit)  v26"
        package = {"name": "some_lib", "version": "1.0.0", "files": [str(payload)],
                   "target": target, "relinked": True, "skipped_hooks": [], "skipped_groups": []}
        plan = {"labview_target": target, "requirements": [{"name": "some_lib", "version": "1.0.0"}]}
        record = installed / "some_lib.json"
        for key, value in (("target", "LabVIEW 2025 (64-bit)  v25"), ("relinked", "true"),
                           ("skipped_hooks", None), ("skipped_groups", None)):
            with self.subTest(key=key):
                record.write_text(json.dumps({**package, key: value}))
                with self.assertRaises(ValueError):
                    ADAPTER.verify_inventory(plan, [package], installed)
        record.write_text(json.dumps(package))
        (installed / "duplicate.json").write_text(json.dumps(package))
        with self.assertRaisesRegex(ValueError, "Unexpected package"):
            ADAPTER.verify_inventory(plan, [package], installed)

    def test_integer_internal_target_version_is_captured_and_normalized(self):
        plan = self.root / "requirements.json"
        plan.write_text(json.dumps(ADAPTER.make_plan(self.root)))
        records = [{"name": "core_lib", "version": "1.0.0"}]
        with patch.object(ADAPTER, "supported_architecture", return_value="amd64"), \
                patch.object(ADAPTER, "run_lvpm", side_effect=["LabVIEW 2026 (64-bit)  v26",
                    "resolved 1 package:\n  core_lib 1.0.0\n\n", "resolved 1 package:\n  core_lib 1.0.0\n\n",
                    "resolved 1 package:\n  core_lib 1.0.0\n\n", ""]), \
                patch.object(ADAPTER, "cached_archives", return_value=records) as archives, \
                patch.object(ADAPTER, "verify_inventory", return_value=records) as inventory:
            report = ADAPTER.install(plan, self.root / "report")
        self.assertEqual(archives.call_args.args[3], "26.0")
        self.assertEqual(report["labview_internal_version"], "26.0")
        self.assertEqual(report["labview_target"], "LabVIEW 2026 (64-bit)  v26")
        self.assertEqual(inventory.call_args.args[0]["labview_target"], report["labview_target"])

    def test_linux_x86_64_architecture_is_normalized(self):
        for machine in ("x86_64", "AMD64"):
            with self.subTest(machine=machine), patch.object(ADAPTER.platform, "system", return_value="Linux"), \
                    patch.object(ADAPTER.platform, "machine", return_value=machine):
                self.assertEqual(ADAPTER.supported_architecture(), "amd64")

    def test_completed_install_records_supported_host_architecture(self):
        plan = self.root / "requirements.json"
        plan.write_text(json.dumps(ADAPTER.make_plan(self.root)))
        output = self.root / "installed-report"
        records = [{"name": "core_lib", "version": "1.0.0"}]
        with patch.object(ADAPTER.platform, "system", return_value="Linux"), \
                patch.object(ADAPTER.platform, "machine", return_value="x86_64"), \
                patch.object(ADAPTER, "run_lvpm", side_effect=[
                    "LabVIEW 2026 (64-bit) v26.0", "resolved 1 package:\n  core_lib 1.0.0\n\n",
                    "resolved 1 package:\n  core_lib 1.0.0\n\n",
                    "resolved 1 package:\n  core_lib 1.0.0\n\n", ""]), \
                patch.object(ADAPTER, "cached_archives", return_value=records), \
                patch.object(ADAPTER, "verify_inventory", return_value=records) as verification:
            report = ADAPTER.install(plan, output)
        verification.assert_called_once()
        self.assertEqual(report["platform"], "linux")
        self.assertEqual(report["architecture"], "amd64")
        self.assertEqual(json.loads((output / "inventory.json").read_text()), report)

    def test_local_recheck_rejects_dropped_package_before_real_install(self):
        plan = self.root / "requirements.json"
        plan.write_text(json.dumps(ADAPTER.make_plan(self.root)))
        with patch.object(ADAPTER, "supported_architecture", return_value="amd64"), \
                patch.object(ADAPTER, "run_lvpm", side_effect=["LabVIEW 2026 (64-bit) v26.3",
                    "resolved 1 package:\n  core_lib 1.0.0\n\n", "resolved 1 package:\n  core_lib 1.0.0\n\n",
                    "resolved 1 package:\n  other_lib 1.0.0\n\n"]) as command, \
                patch.object(ADAPTER, "cached_archives", return_value=[{"name": "core_lib", "version": "1.0.0"}]):
            with self.assertRaisesRegex(ValueError, "complete verified package closure"):
                ADAPTER.install(plan, self.root / "report")
        self.assertEqual(command.call_count, 4)

    def test_transitive_first_wins_cannot_replace_an_exact_root_pin(self):
        plan = self.root / "requirements.json"
        plan.write_text(json.dumps(ADAPTER.make_plan(self.root)))
        records = [{"name": "core_lib", "version": "1.0.0"}]
        with patch.object(ADAPTER, "supported_architecture", return_value="amd64"), \
                patch.object(ADAPTER, "run_lvpm", side_effect=["LabVIEW 2026 (64-bit) v26.3",
                    "resolved 1 package:\n  core_lib 1.0.0\n\n", "resolved 1 package:\n  core_lib 2.0.0\n\n",
                    "resolved 1 package:\n  core_lib 1.0.0\n\n", ""]) as command, \
                patch.object(ADAPTER, "cached_archives", return_value=records) as archives, \
                patch.object(ADAPTER, "verify_inventory", return_value=records):
            ADAPTER.install(plan, self.root / "report")
        self.assertEqual(archives.call_args.args[1], records)
        for index in (1, 3, 4):
            self.assertIn("--no-deps", command.call_args_list[index].args[0])
        self.assertIn("--hooks", command.call_args_list[-1].args[0])
        self.assertIn("--relink", command.call_args_list[-1].args[0])

    def test_root_pin_drift_fails_before_discovery_or_install(self):
        plan = self.root / "requirements.json"
        plan.write_text(json.dumps(ADAPTER.make_plan(self.root)))
        with patch.object(ADAPTER, "supported_architecture", return_value="amd64"), \
                patch.object(ADAPTER, "run_lvpm", side_effect=["LabVIEW 2026 (64-bit) v26.3",
                    "resolved 1 package:\n  core_lib 2.0.0\n\n"]) as command, \
                patch.object(ADAPTER, "cached_archives") as archives:
            with self.assertRaisesRegex(ValueError, "Exact declared dependency.*core_lib=1.0.0"):
                ADAPTER.install(plan, self.root / "report")
        self.assertEqual(command.call_count, 2)
        archives.assert_not_called()

    def test_unsupported_hosts_fail_before_installation_side_effects(self):
        for system, machine in (("Darwin", "x86_64"), ("Windows", "AMD64"),
                                ("Linux", "aarch64"), ("Linux", "i686")):
            with self.subTest(system=system, machine=machine), \
                    patch.object(ADAPTER.platform, "system", return_value=system), \
                    patch.object(ADAPTER.platform, "machine", return_value=machine), \
                    patch.object(ADAPTER, "run_lvpm") as install:
                output = self.root / "unsupported-install"
                with self.assertRaisesRegex(ValueError, "requires Linux x86-64"):
                    ADAPTER.install(self.root / "missing-plan.json", output)
                install.assert_not_called()
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
