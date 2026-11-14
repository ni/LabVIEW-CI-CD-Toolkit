#!/usr/bin/env python3
"""Linux source staging and target inventory regression fixtures."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("linux_worker", Path(__file__).parent / "vipm/linux-worker.py")
WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKER)


class LinuxWorkerTests(unittest.TestCase):
    def test_real_example_stages_vipc_and_dragon_reproducibly(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            plan = WORKER.stage(ROOT, destination, 'a' * 40)
            self.assertEqual({item['path'] for item in plan['files']},
                             {'example/Dependencies.vipc', 'example/Example.dragon',
                              '.github/labview/antidoc/antidoc.vipc', '.github/labview/experimental.vipc',
                              '.github/labview/vipm/ci-tooling.vipc'})
            self.assertEqual(len(WORKER.safe_plan(ROOT)['requirements']), 35)
            dragon = next(item for item in plan['files'] if item['path'].endswith('.dragon'))
            staged = destination / dragon['staged_name']
            before = staged.read_bytes()
            self.assertIn(('oglib_boolean', '6.0.0.9'), WORKER.vipc_specs(staged))
            self.assertEqual(plan, WORKER.stage(ROOT, destination, 'a' * 40))
            self.assertEqual(before, staged.read_bytes())

    def test_tooling_packages_cannot_disappear_from_verified_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, labels in [('ci-tooling.vipc', ['palette-1.1-1', 'shared-1.0']),
                                     ('project-example.vipc', ['shared-2.0'])]:
                with zipfile.ZipFile(root / filename, 'w') as archive:
                    archive.writestr('config.xml', '<Root>' + ''.join(
                        '<Package><Name>' + label + '</Name></Package>' for label in labels) + '</Root>')
            value = WORKER.observation('Using LabVIEW 2026 (64-bit)\nshared v2.0\n', '2026', True)
            with self.assertRaisesRegex(ValueError, 'palette-1.1-1'):
                WORKER.verify_project_pins(value, root)
            value['packages'].append({'name': 'palette', 'version': '1.1-1'})
            WORKER.verify_project_pins(value, root)

    def test_dragon_conflicts_and_unsupported_dependencies_fail_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.github').mkdir()
            (root / '.github/labview-ci.yml').write_text('config: {labviewVersion: "2026"}\n')
            with zipfile.ZipFile(root / 'test.vipc', 'w') as archive:
                archive.writestr('config.xml', '<Root><Package><Name>shared-1.0</Name></Package></Root>')
            for declaration in ('[vipm.dependencies]\nshared="2.0"',
                                '[nipm.dependencies]\nunsupported="1.0"',
                                '[project]\nlabview-version=2025',
                                '[vipm.feeds]\nprivate="https://example.test"'):
                (root / 'test.dragon').write_text(declaration)
                with self.assertRaises(ValueError):
                    WORKER.stage(root, root / 'out', 'a' * 40)

    def test_inventory(self):
        for banner in ("Using LabVIEW 2026 (64-bit)", "LabVIEW 2026 (64-bit)"):
            value = WORKER.observation(banner + "\npackage_id v1.1-1\n", "2026", True)
            WORKER.validate(value, "2026", True)
            self.assertEqual(value["packages"], [{"name": "package_id", "version": "1.1-1"}])
        for banner in ("", "Requested LabVIEW 2026 (64-bit)", "Using LabVIEW 2025 (64-bit)", "Using LabVIEW 2026 (32-bit)",
                       "Using LabVIEW 2026 (64-bit)\nUsing LabVIEW 2025 (64-bit)"):
            value = WORKER.observation(banner + "\npackage_id v1.1-1", "2026", True)
            with self.assertRaises(ValueError):
                WORKER.validate(value, "2026", True)
            self.assertEqual(value["packages"], [])
        value = WORKER.observation("failure", "2026", False, False)
        WORKER.validate(value, "2026", False)
        self.assertEqual(value["status"], "unavailable")
        self.assertEqual(value["packages"], [])

    def test_baseline_empty_inventory_and_failed_command_claim_nothing(self):
        for raw, succeeded in (("Using LabVIEW 2026 (64-bit)\n", True),
                               ("Using LabVIEW 2026 (64-bit)\npackage_id v1.1-1\n", False)):
            value = WORKER.observation(raw, "2026", False, succeeded)
            WORKER.validate(value, "2026", False)
            self.assertEqual(value["status"], "unavailable")
            self.assertFalse(value["target_verified"])
            self.assertEqual(value["packages"], [])

    def test_bom_inventory_cli_and_exact_project_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / "project-test.vipc", "w") as archive:
                archive.writestr("config.xml", "<Root><Package><Name>package_id-1.1-1</Name></Package></Root>")
            artifact = root / "observed-inventory.json"
            for package, accepted in (("package_id v1.1-1", True), ("package_id_extra v1.1-1", False),
                                      ("package_id v1.1-2", False)):
                value = WORKER.observation("Using LabVIEW 2026 (64-bit)\n" + package, "2026", True)
                artifact.write_text(json.dumps(value), encoding="utf-8-sig")
                result = subprocess.run([sys.executable, str(Path(WORKER.__file__)), "validate",
                                         "--year", "2026", "--required", "--vipc-dir", str(root),
                                         "--output", str(artifact)], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_claimed_target_cannot_replace_cli_evidence(self):
        value = WORKER.observation("Using LabVIEW 2026 (64-bit)\npackage_id v1.1-1", "2026", True)
        value["raw"] = "Using LabVIEW 2025 (64-bit)\npackage_id v1.1-1"
        with self.assertRaises(ValueError):
            WORKER.validate(value, "2026", True)

    def test_stage_exact_paths_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").mkdir()
            config = root / ".github/labview-ci.yml"
            config.write_text('config:\n  labviewVersion: "2026"\n  container:\n    vipc:\n'
                              '      - {path: a/same.vipc, monitor: true, monitorOn: linux}\n'
                              '      - {path: b/same.vipc, monitor: true, monitorOn: linux}\n'
                              '    dragon: []\n')
            for name in ("a", "b"):
                (root / name).mkdir()
                with zipfile.ZipFile(root / name / "same.vipc", "w") as archive:
                    archive.writestr("config.xml", "<Root><Package><Name>package_id-1.1-1</Name></Package></Root>")
            result = WORKER.stage(root, root / "out", "a" * 40)
            self.assertEqual(len(result["files"]), 2)
            self.assertEqual(len(list((root / "out").glob("*.vipc"))), 2)
            original = result["files"][0]["sha256"]
            (root / "a/same.vipc").write_bytes(b"changed")
            self.assertNotEqual(WORKER.adapter().digest((root / "a/same.vipc").read_bytes()), original)
            for path in ("../escape.vipc", "/tmp/out.vipc", "a/../b/same.vipc", "a\\same.vipc", "same.vipc"):
                with self.assertRaises(ValueError):
                    WORKER.safe_path(root, path)
            (root / "link").symlink_to(root / "b", target_is_directory=True)
            with self.assertRaises(ValueError):
                WORKER.safe_path(root, "link/same.vipc")

    def test_canonical_platform_selection_and_revision_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").mkdir()
            config = root / ".github/labview-ci.yml"
            config.write_text('container:\n  vipc: [{path: ignored.vipc, monitor: true}]\n'
                              'config:\n  labviewVersion: "2025"\n  container:\n    vipc:\n'
                              '      - {path: ignored-windows.vipc, monitor: true, monitorOn: windows}\n'
                              '      - {path: selected.vipc, monitor: true, monitorOn: windows+linux}\n'
                              '    dragon: []\n')
            with zipfile.ZipFile(root / "selected.vipc", "w") as archive:
                archive.writestr("config.xml", "<Root><Package><Name>package_id-1.1-1</Name></Package></Root>")
            plan = WORKER.stage(root, root / "out", "A" * 40)
            self.assertEqual(plan["dependency_sha"], "a" * 40)
            self.assertEqual(plan["labview_version"], "2025")
            self.assertEqual([source["path"] for source in plan["files"]], ["selected.vipc"])
            self.assertEqual(plan["config_sha256"], WORKER.adapter().digest(config.read_bytes()))
            self.assertEqual(plan["files"][0]["sha256"], WORKER.adapter().digest((root / "selected.vipc").read_bytes()))
            (root / "selected.vipc").unlink()
            (root / "other").mkdir()
            (root / "other/selected.vipc").write_bytes(b"not an implicit replacement")
            with self.assertRaises(ValueError):
                WORKER.stage(root, root / "out", "a" * 40)

    def test_safe_lvpm_plan_rejects_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/labview/vipm").mkdir(parents=True)
            (root / ".github/labview-ci.yml").write_text('config:\n  container:\n    vipc: []\n    dragon: []\n')
            core = root / ".github/labview/vipm/ci-tooling.vipc"
            with zipfile.ZipFile(core, "w") as archive:
                archive.writestr("config.xml", "<Root><Package><Name>package_id-1.1-1</Name></Package></Root>")
            self.assertEqual(WORKER.safe_plan(root), WORKER.adapter().make_plan(root))
            core.rename(root / "real.vipc")
            core.symlink_to(root / "real.vipc")
            with self.assertRaisesRegex(ValueError, "symlink"):
                WORKER.safe_plan(root)

    def test_linux_publication_uses_validated_digest_and_parent_cli(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/build-labview-linux-image.yml").read_text())
        steps = workflow["jobs"]["build-and-push"]["steps"]
        build = next(step for step in steps if step.get("name") == "Build and push LabVIEW Linux CI image")
        manifest = next(step for step in steps if step.get("name") == "Generate worker manifest")
        publish = next(step for step in steps if step.get("name") == "Publish verified Linux worker aliases")
        self.assertNotIn("crane tag", build["run"])
        self.assertIn('docker push "${base}:${worker_version}"', build["run"])
        self.assertLess(steps.index(manifest), steps.index(publish))
        for argument in ("--observed-inventory ci-out/worker/observed-inventory.json",
                         "--labview-bitness 64", '--dependency-sha "$DEPENDENCY_SHA"',
                         '--git-sha "${{ github.sha }}"'):
            self.assertIn(argument, manifest["run"])
        self.assertIn('crane tag "$image_ref" "$tag"', publish["run"])
        self.assertIn('image_ref=$(cat ci-out/linux-verified-image.txt)', publish["run"])
        self.assertIn('"${tag}-deps-${date_tag}"', build["run"])
        self.assertNotIn("labview-target.txt", manifest["run"])

    def test_baseline_only_client_build_uses_qualified_base(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/build-labview-linux-image.yml").read_text())
        steps = workflow["jobs"]["build-and-push"]["steps"]
        step = next(step for step in steps if step.get("name") == "Compute worker version")
        script = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        script = script.replace("import hashlib, json, pathlib, os, re, subprocess",
            "import hashlib, json, pathlib, os, re, subprocess\n"
            "subprocess.check_output = lambda *a, **k: 'sha256:' + 'b' * 64")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.github/labview-ci').mkdir(parents=True)
            (root / '.github/labview-ci/catalog.json').write_bytes((ROOT / '.github/labview-ci/catalog.json').read_bytes())
            output = root / 'output'
            result = subprocess.run([sys.executable, '-c', script], cwd=root,
                env={**os.environ, 'IS_SOURCE': 'false', 'VIPC_COUNT': '0',
                     'LABVIEW_VERSION': '2026', 'GITHUB_OUTPUT': str(output)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertTrue(values['base_image'].startswith('nationalinstruments/labview:'))
            self.assertTrue(values['ni_tag'].endswith('@sha256:' + 'b' * 64))
            self.assertEqual(values['release_id'], values['default_release'])
        build = next(step for step in steps if step.get('name') == 'Build and push LabVIEW Linux CI image')
        self.assertNotIn('crane copy', build['run'])

    def test_parent_manifest_cli_accepts_linux_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = WORKER.observation("Using LabVIEW 2026 (64-bit)\npackage_id v1.1-1\n", "2026", True)
            evidence = root / "observed-inventory.json"
            evidence.write_text(json.dumps(inventory), encoding="utf-8-sig")
            raw = root / "vipm-list.txt"
            raw.write_text(inventory["raw"])
            nipkg = root / "nipkg-list.txt"
            nipkg.write_text("")
            dpkg = root / "dpkg-list.txt"
            dpkg.write_text("ni-vialinux-labview-support 26.1.0\n")
            vipc = root / "project-test.vipc"
            with zipfile.ZipFile(vipc, "w") as archive:
                archive.writestr("config.xml", "<Root><Package><Name>package_id-1.1-1</Name></Package></Root>")
            output = root / "manifest"
            result = subprocess.run([sys.executable, str(ROOT / ".github/labview/build-worker-manifest.py"),
                "--platform", "linux", "--version", "linux-fixture", "--labview-version", "2026",
                "--labview-bitness", "64", "--observed-inventory", str(evidence),
                "--nipkg-list", str(nipkg), "--dpkg-list", str(dpkg), "--vipm-list", str(raw), "--vipc", str(vipc),
                "--worker-layer", "repo-vipc-layer", "--base-image", "example.test/base:2026",
                "--base-digest", "sha256:" + "b" * 64,
                "--image-ref", "example.test/worker:linux-fixture@sha256:" + "c" * 64,
                "--git-sha", "a" * 40, "--dependency-sha", "d" * 40,
                "--pages-url", "https://example.test", "--run-url", "https://example.test/run/1",
                "--out-dir", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["dpkg_packages"], [{"name": "ni-vialinux-labview-support", "version": "26.1.0"}])
            self.assertEqual(manifest["schema"], 3)
            self.assertEqual(manifest["platform"], "linux")
            self.assertEqual(manifest["labview_version"], "2026")
            self.assertEqual(manifest["labview_bitness"], "64")
            self.assertEqual(manifest["git_sha"], "a" * 40)
            self.assertEqual(manifest["dependency_sha"], "d" * 40)
            self.assertEqual(manifest["vipm_inventory"], inventory)
            self.assertEqual(manifest["vipm_packages"], inventory["packages"])
            self.assertEqual(manifest["vipc"][0]["file"], vipc.name)
            self.assertIn("package_id", (output / "manifest.html").read_text())

    def test_latest_manifest_only_replaces_default_release(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/build-labview-linux-image.yml").read_text())
        step = next(step for step in workflow["jobs"]["build-and-push"]["steps"]
                    if step.get("name") == "Generate worker manifest")
        script = step["run"].split('> ci-out/linux-verified-image.txt\n', 1)[1]
        for mode in ("replace", "new"):
            for release in ("2026q1", "2025q3"):
                with self.subTest(mode=mode, release=release), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    latest = root / "ci-out/workers-pages/workers/linux/latest.json"
                    latest.parent.mkdir(parents=True)
                    rendered = script
                    for expression, value in {
                        "github.event.inputs.update_mode || 'replace'": mode,
                        "steps.ver.outputs.release_id": release,
                        "steps.ver.outputs.default_release": "2026q1",
                        "env.PAGES_URL": "https://example.test",
                        "github.sha": "a" * 40,
                    }.items():
                        rendered = rendered.replace("${{ " + expression + " }}", value)
                    for existing in (False, True):
                        if existing:
                            latest.write_text('{"version":"previous"}')
                        result = subprocess.run(["bash", "-euc", rendered], cwd=root,
                            env={**os.environ, "worker_version": "linux-fixture"},
                            capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        replaces = mode == "replace" and release == "2026q1"
                        self.assertEqual(latest.exists(), existing or replaces)
                        if latest.exists():
                            self.assertEqual(json.loads(latest.read_text())["version"],
                                             "linux-fixture" if replaces else "previous")
                            latest.unlink()

    def test_push_gate_ignores_noncanonical_container(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/build-labview-linux-image.yml").read_text())
        step = next(step for step in workflow["jobs"]["gate"]["steps"] if step.get("id") == "decide")
        script = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        script = script.replace("import os, subprocess, pathlib, yaml", "import os, subprocess, pathlib, yaml\n"
                                "subprocess.run = lambda *args, **kwargs: type('Result', (), "
                                "{'returncode': 0, 'stdout': 'selected.vipc\\n'})()")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").mkdir()
            output = root / "output"
            (root / ".github/labview-ci.yml").write_text('config:\n  labviewVersion: "2026"\n'
                'container:\n  vipc: [{path: selected.vipc, monitor: true, monitorOn: linux}]\n')
            result = subprocess.run([sys.executable, "-c", script], cwd=root,
                env={**os.environ, "EVENT_NAME": "push", "BEFORE": "a" * 40,
                     "SHA": "b" * 40, "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), "build=false\n")


if __name__ == "__main__":
    unittest.main()
