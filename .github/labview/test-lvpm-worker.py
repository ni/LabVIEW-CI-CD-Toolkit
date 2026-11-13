#!/usr/bin/env python3
"""Offline security, routing, and workflow contract tests for LVPM candidates."""

import ast
import base64
import copy
import fnmatch
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("lvpm_worker", ROOT / ".github/labview/lvpm/worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        (self.repo / ".github").mkdir()
        self.repository = "Example/Project"
        self.fingerprint = "a" * 64
        self.digest = "sha256:" + "b" * 64
        self.base_digest = "sha256:" + "c" * 64
        self.inputs_hash = "d" * 64
        self.tag = worker.candidate_tag(self.fingerprint, self.base_digest, self.inputs_hash, 123, 1)
        self.selection = f"{self.tag}@{self.digest}"
        self.plan = {"schema": 1, "labview_version": "2026", "requirements_fingerprint": self.fingerprint,
                     "requirements": [{"name": "example", "version": "1.2.3", "source_files": ["test.vipc"]}]}
        self.manifest = {"schema": 1, "installer": "lvpm", "platform": "linux", "architecture": "amd64", "tag": self.tag,
                         "repository": self.repository, "tool_revision": worker.TOOL_REVISION,
                         "manifest_path": f"workers/linux-lvpm/{self.tag}/manifest.json",
                         "requirements_fingerprint": self.fingerprint, "labview_version": "2026",
                         "labview_target": "LabVIEW 2026 (64-bit)  v26", "labview_internal_version": "26.0",
                         "image_digest": self.digest,
                         "image_ref": f"ghcr.io/example/project-labview:{self.selection}",
                         "base_image": f"nationalinstruments/labview:2026q3-linux@{self.base_digest}",
                         "base_digest": self.base_digest, "build_inputs_sha256": self.inputs_hash,
                         "verification": {"status": "passed", "scope": "package-files", "activity_checks": "not_run",
                                          "hook_error_output": "not_observable_by_upstream"},
                         "packages": [{"name": "example", "version": "1.2.3", "sha256": "e" * 64,
                                       "target": "LabVIEW 2026 (64-bit)  v26",
                                       "files": ["/usr/local/natinst/LabVIEW-2026/vi.lib/example.vi"]}],
                         "workflow_run": {"id": 123, "attempt": 1, "head_sha": "f" * 40,
                                          "workflow_path": worker.WORKFLOW}}
        self.run = {"id": 123, "run_attempt": 1, "head_sha": "f" * 40, "head_branch": "main",
                    "workflow_id": 456, "path": worker.WORKFLOW, "event": "workflow_dispatch",
                    "status": "completed", "conclusion": "success",
                    "repository": {"full_name": self.repository}, "head_repository": {"full_name": self.repository}}
        self.metadata = {"full_name": self.repository, "default_branch": "main"}
        self.workflow = {"path": worker.WORKFLOW, "id": 456}
        for target, options in (("github_json", {"side_effect": self.api}),
                                ("current_plan", {"return_value": self.plan}),
                                ("build_inputs_hash", {"return_value": self.inputs_hash}),
                                ("verify_registry", {})):
            mocked = patch.object(worker, target, **options)
            setattr(self, target, mocked.start())
            self.addCleanup(mocked.stop)

    def api(self, endpoint):
        prefix = f"repos/{self.repository}"
        if endpoint == prefix:
            return self.metadata
        if endpoint == f"{prefix}/contents/workers/linux-lvpm/{self.tag}/manifest.json?ref=gh-pages":
            return {"type": "file", "path": self.manifest["manifest_path"], "encoding": "base64",
                    "content": base64.b64encode(json.dumps(self.manifest).encode()).decode()}
        if endpoint == f"{prefix}/actions/runs/123":
            return self.run
        if endpoint == f"{prefix}/actions/workflows/build-labview-lvpm-image.yml":
            return self.workflow
        raise AssertionError(f"Unexpected API endpoint: {endpoint}")

    def verify(self):
        return worker.verify(self.repo, self.selection, self.repository)

    def config(self, container, **config):
        data = {"config": {"container": container, **config}}
        (self.repo / ".github/labview-ci.yml").write_text(yaml.safe_dump(data))

    def test_valid_candidate_uses_authenticated_pages_and_exact_registry_digest(self):
        self.assertEqual(self.verify(), self.manifest)
        self.verify_registry.assert_called_once_with("ghcr.io/example/project-labview", self.tag, self.digest)
        self.current_plan.assert_called_once_with(self.repo, "2026")

    def test_local_freshness_is_current_without_network_or_runtime_verification(self):
        revision = "f" * 40
        result = worker.local_freshness(self.repo, self.selection, revision, self.manifest)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["revision"], revision)
        self.assertEqual(result["selection"], self.selection)
        self.assertIn("have not been verified", result["reason"])
        self.github_json.assert_not_called()
        self.verify_registry.assert_not_called()

    def test_local_freshness_marks_unchanged_pins_with_changed_inputs_stale(self):
        self.plan["requirements_fingerprint"] = "0" * 64
        self.assertEqual(worker.local_freshness(self.repo, self.selection, "f" * 40, self.manifest)["status"], "stale")
        self.plan["requirements_fingerprint"] = self.fingerprint
        self.build_inputs_hash.return_value = "0" * 64
        result = worker.local_freshness(self.repo, self.selection, "f" * 40, self.manifest)
        self.assertEqual(result["status"], "stale")
        self.assertIn("re-activate", result["reason"])

    def test_local_freshness_rejects_missing_architecture_and_selection_mismatch(self):
        original = copy.deepcopy(self.manifest)
        for manifest in ({key: value for key, value in original.items() if key != "architecture"},
                         {**original, "architecture": "arm64"}, {**original, "image_digest": "sha256:" + "0" * 64}):
            with self.subTest(manifest=manifest):
                result = worker.local_freshness(self.repo, self.selection, "f" * 40, manifest)
                self.assertEqual(result["status"], "stale")

    def test_local_freshness_cannot_guess_when_prerequisites_or_evidence_are_unavailable(self):
        self.assertEqual(worker.local_freshness(self.repo, self.selection, "", self.manifest)["status"], "unknown")
        self.assertEqual(worker.local_freshness(self.repo, self.selection, "f" * 40, None)["status"], "unknown")
        for error in (FileNotFoundError("missing"), subprocess.CalledProcessError(1, "adapter"),
                      KeyError("catalog"), json.JSONDecodeError("invalid", "", 0)):
            with self.subTest(error=error):
                self.current_plan.side_effect = error
                self.assertEqual(worker.local_freshness(self.repo, self.selection, "f" * 40, self.manifest)["status"], "unknown")

    def test_every_identity_field_is_bound(self):
        changes = {"schema": 2, "installer": "vipm", "platform": "windows", "architecture": "arm64", "tag": "linux-latest",
                   "repository": "Foreign/Project", "tool_revision": "0" * 40, "image_digest": "sha256:" + "0" * 64,
                   "image_ref": f"ghcr.io/foreign/project-labview:{self.selection}", "labview_version": "2025",
                   "requirements_fingerprint": "0" * 64, "base_digest": "sha256:" + "0" * 64,
                   "build_inputs_sha256": "0" * 64, "base_image": "nationalinstruments/labview:latest-linux"}
        original = copy.deepcopy(self.manifest)
        for key, value in changes.items():
            with self.subTest(key=key):
                self.manifest = {**original, key: value}
                with self.assertRaises(ValueError):
                    self.verify()
        self.manifest = original

    def test_legacy_manifest_without_architecture_requires_rebuild(self):
        self.manifest.pop("architecture")
        with self.assertRaisesRegex(ValueError, "manifest identity"):
            self.verify()

    def test_namespace_cannot_be_substituted(self):
        self.manifest["manifest_path"] = "workers/linux/latest.json"
        with self.assertRaisesRegex(ValueError, "namespace"):
            self.verify()

    def test_changed_dependencies_are_stale(self):
        self.plan["requirements_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "stale"):
            self.verify()

    def test_changed_adapter_policy_or_build_inputs_are_stale(self):
        self.build_inputs_hash.return_value = "0" * 64
        with self.assertRaisesRegex(ValueError, "build inputs have changed"):
            self.verify()

    def test_changed_labview_config_is_stale(self):
        self.config({}, labviewVersion="2025")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.verify()

    def test_unsupported_requirements_are_not_silently_ignored(self):
        self.current_plan.side_effect = subprocess.CalledProcessError(1, "adapter")
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify()

    def test_rejects_noncanonical_failed_or_unfinished_builds(self):
        changes = {"id": 124, "run_attempt": 2, "head_sha": "0" * 40, "head_branch": "feature",
                   "workflow_id": 789, "path": ".github/workflows/build-labview-linux-image.yml",
                   "event": "push", "status": "in_progress", "conclusion": "failure",
                   "repository": {"full_name": "Foreign/Project"}, "head_repository": {"full_name": "Foreign/Project"}}
        original = copy.deepcopy(self.run)
        for key, value in changes.items():
            with self.subTest(key=key):
                self.run = {**original, key: value}
                with self.assertRaisesRegex(ValueError, "canonical"):
                    self.verify()
        self.run = original

    def test_wrong_workflow_identity_is_rejected(self):
        self.workflow["path"] = ".github/workflows/impostor.yml"
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.verify()

    def test_repository_identity_is_checked(self):
        self.metadata["full_name"] = "Foreign/Project"
        with self.assertRaisesRegex(ValueError, "Repository identity"):
            self.verify()

    def test_file_only_evidence_cannot_claim_runtime_validation(self):
        for key, value in (("status", "failed"), ("scope", "runtime"), ("activity_checks", "passed"),
                           ("hook_error_output", "passed")):
            with self.subTest(key=key):
                original = self.manifest["verification"][key]
                self.manifest["verification"][key] = value
                with self.assertRaisesRegex(ValueError, "verification"):
                    self.verify()
                self.manifest["verification"][key] = original

    def test_inventory_must_include_exact_declared_packages_and_files(self):
        original = copy.deepcopy(self.manifest["packages"])
        variants = [[], [None], original * 2]
        variants += [[{**original[0], key: value}] for key, value in (
            ("version", "9.9.9"), ("name", "other"), ("sha256", "bad"), ("files", []),
            ("files", ["relative.vi"]), ("files", ["/tmp/../escape.vi"]))]
        for packages in variants:
            with self.subTest(packages=packages):
                self.manifest["packages"] = packages
                with self.assertRaises(ValueError):
                    self.verify()

    def test_registry_failure_is_fatal(self):
        self.verify_registry.side_effect = ValueError("digest mismatch")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.verify()

    def test_malformed_and_mutable_selections_are_rejected_without_network(self):
        for candidate in ("", self.tag, "linux-lvpm-latest", "../manifest.json", self.selection.upper(),
                          f"ghcr.io/example/project-labview:{self.selection}", self.selection + "\n"):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                worker.verify(self.repo, candidate, self.repository)
        self.github_json.assert_not_called()

    def test_candidate_identity_is_unique_per_base_inputs_and_run_attempt(self):
        identities = [worker.candidate_tag(self.fingerprint, base, inputs, run_id, attempt)
                      for base, inputs, run_id, attempt in (
                          (self.base_digest, self.inputs_hash, 123, 1),
                          ("sha256:" + "0" * 64, self.inputs_hash, 123, 1),
                          (self.base_digest, "0" * 64, 123, 1),
                          (self.base_digest, self.inputs_hash, 124, 1),
                          (self.base_digest, self.inputs_hash, 123, 2))]
        self.assertEqual(len(set(identities)), 5)

    def test_custom_ghcr_package_can_be_verified_in_own_namespace(self):
        self.manifest["image_ref"] = f"ghcr.io/example/custom:{self.selection}"
        self.assertEqual(worker.verify(self.repo, self.selection, self.repository, "custom"), self.manifest)
        with self.assertRaises(ValueError):
            worker.verify(self.repo, self.selection, self.repository, "ghcr.io/foreign/custom")
        with self.assertRaises(ValueError):
            worker.verify(self.repo, self.selection, self.repository, "ghcr.io/example/custom")

    def test_action_override_wins_over_linux_default_and_is_verified(self):
        self.config({"linuxUse": f'linux-lvpm-{"0" * 64}@{self.digest}', "actions": {"masscompile": self.selection}, "use": "2026"})
        with patch.object(worker, "verify", return_value=self.manifest) as verify:
            self.assertEqual(worker.resolve(self.repo, "masscompile", self.repository), self.selection)
            verify.assert_called_once_with(self.repo, self.selection, self.repository, None)

    def test_explicit_non_lvpm_action_preserves_custom_selection(self):
        self.config({"linuxUse": self.selection, "actions": {"masscompile": "custom-tag"}})
        with patch.object(worker, "verify") as verify:
            self.assertEqual(worker.resolve(self.repo, "masscompile", self.repository), "custom-tag")
            verify.assert_not_called()

    def test_linux_default_wins_over_legacy_shared_default(self):
        self.config({"linuxUse": self.selection, "use": "windows-custom"})
        self.assertEqual(worker.resolve(self.repo, "vi-analyzer", self.repository), self.selection)

    def test_legacy_defaults_and_no_configuration_remain_unchanged(self):
        self.assertEqual(worker.resolve(self.repo, "builds", self.repository), "")
        for selection in ("latest", "base", "none", "linux-custom", "2026", "windows-custom", "custom-lvpm-tooling"):
            with self.subTest(selection=selection):
                self.config({"use": selection})
                self.assertEqual(worker.resolve(self.repo, "builds", self.repository), selection)

    def test_malformed_explicit_lvpm_override_cannot_bypass_verification(self):
        self.config({"actions": {"builds": "linux-lvpm-latest"}, "use": "base"})
        with self.assertRaises(ValueError):
            worker.resolve(self.repo, "builds", self.repository)

    def test_multiline_selection_is_rejected(self):
        self.config({"use": "custom\nINJECTED=true"})
        with self.assertRaises(ValueError):
            worker.resolve(self.repo, "builds", self.repository)

    def test_reserved_shorthand_cannot_reach_workflow_tag_prefixing(self):
        for selection in (self.selection.removeprefix("linux-"), "lvpm-latest", "LVPM-latest"):
            with self.subTest(selection=selection):
                self.config({"use": selection})
                self.github_json.reset_mock()
                with self.assertRaisesRegex(ValueError, "LVPM selection must"):
                    worker.resolve(self.repo, "builds", self.repository)
                self.github_json.assert_not_called()

    def test_shell_payloads_are_rejected_before_outputs_or_verification(self):
        for selection in ("$(touch injected)", "custom`id`", "custom;id", "custom'", 'custom"',
                          "custom tag", "custom&tag", "custom\\tag", "custom\tvalue"):
            with self.subTest(selection=selection):
                self.config({"actions": {"builds": selection}})
                self.github_json.reset_mock()
                with self.assertRaisesRegex(ValueError, "without shell syntax"):
                    worker.resolve(self.repo, "builds", self.repository)
                self.github_json.assert_not_called()

    def test_linux_default_rejects_noncanonical_values_even_when_overridden(self):
        for selection in ("latest", "base", self.tag, self.selection.removeprefix("linux-"),
                          "$(id)", {"tag": self.selection}):
            with self.subTest(selection=selection):
                self.config({"linuxUse": selection, "actions": {"builds": "safe-custom"}})
                with self.assertRaisesRegex(ValueError, "linuxUse requires a canonical"):
                    worker.resolve(self.repo, "builds", self.repository)

    def test_snapshots_preserve_original_selection_until_explicit_linux_opt_in(self):
        for selection in ("", "base", "latest", "custom-tag"):
            with self.subTest(selection=selection):
                self.config({"use": selection, "actions": {"snapshots": "legacy-ignored"}})
                self.assertEqual(worker.resolve(self.repo, "snapshots", self.repository), "")
        self.config({"linuxUse": self.selection, "use": "ignored"})
        self.assertEqual(worker.resolve(self.repo, "snapshots", self.repository), self.selection)
        self.config({"linuxUse": self.selection, "actions": {"snapshots": "explicit-custom"}})
        self.assertEqual(worker.resolve(self.repo, "snapshots", self.repository), "explicit-custom")


class NativePackageInventoryTests(unittest.TestCase):
    def test_removed_and_unconfigured_packages_do_not_count_as_installed(self):
        raw = ('installed\tni-vialinux-labview-support\t26.3.0\n'
               'config-files\tremoved-package\t1.0\n'
               'unpacked\tincomplete-package\t2.0\n')
        self.assertEqual(worker.native_packages(raw), [{'name':'ni-vialinux-labview-support', 'version':'26.3.0'}])
        for invalid in ('', 'ni-vialinux-labview-support 26.3.0', 'installed\tbad name\t1.0',
                        'installed\tmissing-version\t', 'config-files\tremoved-package\t1.0'):
            with self.assertRaises(ValueError):
                worker.native_packages(invalid)

    @unittest.skipUnless(shutil.which('dpkg-query'), 'Native dpkg query requires Linux')
    def test_actual_installed_status_query_and_workflow_capture(self):
        workflow = yaml.safe_load((ROOT / worker.WORKFLOW).read_text())
        step = next(step for step in workflow['jobs']['candidate']['steps']
                    if step['name'] == 'Bind the installed file inventory to the exact published digest')
        query_format = '${db:Status-Status}\t${Package}\t${Version}\n'
        self.assertIn("-W '-f=${db:Status-Status}\\t${Package}\\t${Version}\\n'", step['run'])
        raw = subprocess.check_output(['dpkg-query', '-W', '-f=' + query_format], text=True)
        installed = worker.native_packages(raw)
        self.assertTrue(any(item['name'] == 'dpkg' for item in installed))
        self.assertIn("'dpkg_packages': native", step['run'])
        self.assertIn('docker run --rm --network none --entrypoint /usr/bin/dpkg-query "$image_ref"', step['run'])


class NetworkAndWorkflowTests(unittest.TestCase):
    def test_native_linux_installer_is_targeted_and_fails_closed(self):
        script = (ROOT / '.github/labview/vipm/install-vipc-linux.sh').read_text()
        mocks = (
            "find_vipm() { printf 'vipm\\n'; }\n"
            "setup_display() { :; }\n"
            "start_labview() { LABVIEW_PID=$$; }\n"
            "kill() { if [ \"$EXIT_DURING_REFRESH\" = 1 ] && [ -f \"$CALLS.refresh\" ]; then return 1; fi; builtin kill \"$@\"; }\n"
            "vipc_package_specs() { printf 'some_lib-1.1-1\\n'; }\n"
            "timeout() { printf '%s\\n' \"$*\" >> \"$CALLS\"; shift 2; \"$@\"; }\n"
            "vipm() { case \" $* \" in\n"
            "*' refresh '*) : > \"$CALLS.refresh\" ;;\n"
            "*' install '*) return \"$INSTALL_EXIT\" ;;\n"
            "*' list '*) printf '%s\\n' \"$INSTALLED_INVENTORY\"; return \"$LIST_EXIT\" ;;\n"
            "*) return 0 ;; esac; }\n")
        script = script.replace('VIPM_BIN="$(find_vipm || true)"', mocks + '\nVIPM_BIN="$(find_vipm || true)"')
        script = script.replace("\nprepare_unattended_hooks\n", '\nprepare_unattended_hooks "$VIPC_DIR/labview-data"\n')
        inventory = 'Using LabVIEW 2026 (64-bit)\nsome_lib v1.1-1'
        cases = (
            ('0', '0', '0', inventory, True),
            ('2', '0', '0', inventory, False),
            ('0', '1', '0', inventory, False),
            ('0', '0', '1', inventory, True),
            ('2', '0', '1', inventory, False),
            ('0', '1', '1', inventory, False),
            ('0', '0', '1', '', False),
            ('0', '0', '1', inventory.replace('1.1-1', '1.1-2'), False),
            ('0', '0', '1', inventory.replace('some_lib v1.1-1', 'other_lib v1.1-1'), False),
            ('0', '0', '1', inventory.replace('2026', '2025'), False),
            ('0', '0', '1', inventory.replace('64-bit', '32-bit'), False),
        )
        for install_exit, list_exit, exit_during_refresh, installed_inventory, success in cases:
            with self.subTest(install=install_exit, inventory=list_exit, exited=exit_during_refresh,
                              installed=installed_inventory), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                import zipfile
                with zipfile.ZipFile(repo / 'project-deps.vipc', 'w') as archive:
                    archive.writestr('config.xml', '<Root><Package><Name>some_lib-1.1-1</Name></Package></Root>')
                evidence = repo / 'evidence'
                result = subprocess.run(['bash', '-c', script, str(ROOT / '.github/labview/vipm/install-vipc-linux.sh')], env={**os.environ,
                    'VIPC_DIR': str(repo), 'INVENTORY_DIR': str(evidence), 'CALLS': str(repo / 'calls'),
                    'INSTALL_EXIT': install_exit, 'LIST_EXIT': list_exit, 'LABVIEW_VERSION': '2026',
                    'EXIT_DURING_REFRESH': exit_during_refresh, 'INSTALLED_INVENTORY': installed_inventory,
                    'VIPM_SERIAL_NUMBER': '', 'LV_RTE_HEADLESS': '1'}, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                calls = (repo / 'calls').read_text()
                self.assertIn('--kill-after=30s 1200s vipm', calls)
                self.assertIn('--labview-version 2026 --labview-bitness 64 install some_lib@1.1-1', calls)
                self.assertEqual(calls.count(' install '), 1)
                self.assertEqual((evidence / 'observed-inventory.json').exists(), install_exit == '0')
                if install_exit == '0':
                    self.assertIn('--labview-version 2026 --labview-bitness 64 list --installed', calls)
                    self.assertEqual('Initial LabVIEW process exited' in result.stdout, exit_during_refresh == '1')
                else:
                    self.assertNotIn(' list ', calls)
                if success:
                    self.assertIn('--labview-version 2026 --labview-bitness 64 list --installed', calls)
                    observed = json.loads((evidence / 'observed-inventory.json').read_text())
                    self.assertTrue(observed['target_verified'])
                    self.assertTrue(observed['required_project'])
                    self.assertEqual(observed['packages'], [{'name': 'some_lib', 'version': '1.1-1'}])

    def test_linux_gate_executes_nested_monitor_configuration(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/build-labview-linux-image.yml').read_text())
        step = next(step for step in workflow['jobs']['gate']['steps'] if step.get('id') == 'decide')
        script = step['run'].split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        prefix = ("import subprocess\nfrom types import SimpleNamespace\n"
                  "subprocess.run=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='Deps.vipc\\n')\n")
        for platforms, enabled, expected in ((['linux'], True, 'true'), ('windows+linux', True, 'true'),
                                              (['windows'], True, 'false'), (['linux'], False, 'false')):
            with self.subTest(platforms=platforms, enabled=enabled), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                (repo / '.github').mkdir()
                (repo / '.github/labview-ci.yml').write_text(yaml.safe_dump({'config': {'container': {'vipc': [
                    {'path': 'Deps.vipc', 'monitor': enabled, 'monitorOn': platforms}]}}}))
                output = repo / 'output'
                result = subprocess.run([sys.executable, '-c', prefix + script], cwd=repo,
                    env={**os.environ, 'EVENT_NAME': 'push', 'BEFORE': 'a' * 40, 'SHA': 'b' * 40,
                         'GITHUB_OUTPUT': str(output)}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(), f'build={expected}\n', result.stdout)

    def test_local_cli_treats_client_helpers_as_data_not_executable_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            for relative in (worker.WORKFLOW, '.github/docker/labview-ci-lvpm.Dockerfile',
                             '.github/labview/lvpm/worker.py', '.github/labview/lvpm/adapter.py'):
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('raise RuntimeError("inspected client code must not execute")\n')
            catalog = repo / '.github/labview-ci/catalog.json'
            catalog.parent.mkdir(parents=True)
            catalog.write_bytes((ROOT / '.github/labview-ci/catalog.json').read_bytes())
            core = repo / '.github/labview/vipm/ci-tooling.vipc'
            core.parent.mkdir(parents=True)
            with zipfile.ZipFile(core, 'w') as archive:
                archive.writestr('config.xml', '<Configuration><Package><Name>core_lib-1.0.0</Name></Package></Configuration>')
            plan = worker.current_plan(repo, '2026')
            selection = f'linux-lvpm-{"a" * 64}@sha256:{"b" * 64}'
            manifest = {'schema': 1, 'installer': 'lvpm', 'platform': 'linux', 'architecture': 'amd64',
                'tool_revision': worker.TOOL_REVISION, 'tag': selection.split('@')[0], 'image_digest': selection.split('@')[1],
                'labview_version': '2026', 'labview_target': 'LabVIEW 2026 (64-bit)  v26',
                'labview_internal_version': '26.0', 'requirements_fingerprint': plan['requirements_fingerprint'],
                'build_inputs_sha256': worker.build_inputs_hash(repo), 'packages': [
                    {'name': 'core_lib', 'version': '1.0.0', 'sha256': 'c' * 64, 'files': ['/example/library.vi'],
                     'target': 'LabVIEW 2026 (64-bit)  v26'}],
                'verification': {'status': 'passed', 'scope': 'package-files', 'activity_checks': 'not_run',
                                 'hook_error_output': 'not_observable_by_upstream'}}
            shadow_marker = repo / 'shadow-executed'
            for name in ('pathlib', 'yaml', 'platform', 'sitecustomize'):
                (repo / f'{name}.py').write_text(
                    f'open({str(shadow_marker)!r}, "w").write("executed")\n'
                    'raise RuntimeError("client import shadow must not execute")\n')
            result = subprocess.run([sys.executable, '-I', str(ROOT / '.github/labview/lvpm/worker.py'),
                'freshness', '--repo', str(repo), '--candidate', selection, '--revision', 'f' * 40],
                input=json.dumps(manifest), cwd=repo, env={**os.environ, 'PYTHONPATH': str(repo)},
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'current')
            self.assertFalse(shadow_marker.exists())
            self.assertNotIn('client import shadow', result.stderr)

    def test_fingerprint_covers_consumed_catalog_metadata_not_release_bookkeeping(self):
        catalog = json.loads((ROOT / ".github/labview-ci/catalog.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            for relative in (worker.WORKFLOW, ".github/docker/labview-ci-lvpm.Dockerfile",
                             ".github/labview/lvpm/adapter.py", ".github/labview/lvpm/worker.py"):
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            target = repo / ".github/labview-ci/catalog.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(catalog))
            original = worker.build_inputs_hash(repo)
            release_only = {**catalog, "version": "999.999.999", "history": [], "channel": "test"}
            target.write_text(json.dumps(release_only))
            self.assertEqual(worker.build_inputs_hash(repo), original)
            for key, value in (("linuxTag", "nationalinstruments/labview:2099q1-linux"),
                               ("labviewYear", "2099"), ("status", "unqualified")):
                with self.subTest(key=key):
                    updated = copy.deepcopy(catalog)
                    selected = next(entry for entry in updated["containers"]["supported"]
                                    if entry["id"] == updated["containers"]["default"])
                    selected[key] = value
                    target.write_text(json.dumps(updated))
                    self.assertNotEqual(worker.build_inputs_hash(repo), original)
            updated = copy.deepcopy(catalog)
            selected = next(entry for entry in updated["containers"]["supported"]
                            if entry["id"] == updated["containers"]["default"])
            updated["containers"]["supported"].append({**selected, "id": "alternate"})
            target.write_text(json.dumps(updated))
            self.assertEqual(worker.build_inputs_hash(repo), original)
            updated["containers"]["default"] = "alternate"
            target.write_text(json.dumps(updated))
            self.assertNotEqual(worker.build_inputs_hash(repo), original)

    def test_image_and_inventory_must_both_prove_supported_architecture(self):
        image = {"Os": "linux", "Architecture": "amd64"}
        inventory = {"platform": "linux", "architecture": "amd64"}
        worker.verify_image_platform(image, inventory)
        for actual_image, actual_inventory in (
                ({**image, "Architecture": "arm64"}, inventory),
                ({**image, "Os": "windows"}, inventory),
                (image, {**inventory, "architecture": "arm64"}),
                (image, {**inventory, "platform": "windows"}),
                (image, {"platform": "linux"}),
                ({"Os": "linux"}, inventory)):
            with self.subTest(image=actual_image, inventory=actual_inventory):
                with self.assertRaisesRegex(ValueError, "both prove linux/amd64"):
                    worker.verify_image_platform(actual_image, actual_inventory)

    def test_github_reads_require_explicit_authentication(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "Authenticated"):
            worker.github_json("repos/example/project")

    def test_github_api_is_authenticated_and_host_pinned(self):
        with patch.dict(os.environ, {"GH_TOKEN": "test"}), patch.object(worker.subprocess, "run") as run:
            run.return_value.stdout = '{"ok":true}'
            self.assertEqual(worker.github_json("repos/example/project"), {"ok": True})
            self.assertIn("github.com", run.call_args.args[0])
            self.assertNotIn("test", run.call_args.args[0])

    def test_registry_tag_and_digest_must_both_match(self):
        class Response(io.BytesIO):
            headers = {"Docker-Content-Digest": "sha256:" + "b" * 64}

        token = Response(b'{"token":"registry-token"}')
        tag = Response()
        digest = Response()
        with patch.dict(os.environ, {"GH_TOKEN": "test"}), patch.object(worker, "build_opener") as build:
            build.return_value.open.side_effect = [token, tag, digest]
            worker.verify_registry("ghcr.io/example/project-labview", "linux-lvpm-" + "a" * 64, "sha256:" + "b" * 64)
            requests = [call.args[0] for call in build.return_value.open.call_args_list]
            self.assertEqual([request.method for request in requests[1:]], ["HEAD", "HEAD"])
            self.assertTrue(requests[-1].full_url.endswith("/sha256:" + "b" * 64))

    def test_registry_retargeted_tag_fails_closed(self):
        class Response(io.BytesIO):
            headers = {"Docker-Content-Digest": "sha256:" + "0" * 64}

        with patch.dict(os.environ, {"GH_TOKEN": "test"}), patch.object(worker, "build_opener") as build:
            build.return_value.open.side_effect = [Response(b'{"token":"registry-token"}'), Response()]
            with self.assertRaisesRegex(ValueError, "changed"):
                worker.verify_registry("ghcr.io/example/project-labview", "linux-lvpm-" + "a" * 64, "sha256:" + "b" * 64)

    def test_registry_credentials_never_follow_redirects(self):
        with self.assertRaisesRegex(ValueError, "redirect"):
            worker.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://foreign.example")

    def test_new_workflow_is_manual_only_and_namespace_confined(self):
        text = (ROOT / worker.WORKFLOW).read_text()
        data = yaml.load(text, Loader=yaml.BaseLoader)
        self.assertEqual(list(data["on"]), ["workflow_dispatch"])
        publishers = [step for step in data["jobs"]["candidate"]["steps"] if "publish-gh-pages" in step.get("uses", "")]
        self.assertEqual(publishers[0]["with"]["destination_dir"], "workers/linux-lvpm")
        self.assertEqual(publishers[0]["with"]["publish_dir"], "ci-out/lvpm-pages/workers/linux-lvpm")
        self.assertEqual(text.count('--tag "$IMAGE:$TAG"'), 1)
        self.assertNotIn("linux-latest", text)
        self.assertIn('docker image inspect "$image_ref" > ci-out/lvpm-image.json', text)
        self.assertIn('worker.verify_image_platform(image[0], inventory)', text)
        self.assertLess(text.index('worker.verify_image_platform(image[0], inventory)'),
                        text.index("manifest = {**inventory"))

    def test_linux_update_guards_precede_checkout_and_pin_all_checkouts(self):
        for filename, first_job in (("build-labview-lvpm-image.yml", "candidate"),
                                    ("build-labview-linux-image.yml", "gate")):
            with self.subTest(workflow=filename):
                workflow = yaml.load((ROOT / '.github/workflows' / filename).read_text(), Loader=yaml.BaseLoader)
                self.assertIn('expected_sha', workflow['on']['workflow_dispatch']['inputs'])
                reviewed_input = workflow['on']['workflow_dispatch']['inputs']['expected_sha']
                self.assertEqual(reviewed_input['type'], 'string')
                self.assertEqual(reviewed_input['required'], 'false')
                self.assertEqual(reviewed_input['default'], '')
                steps = workflow['jobs'][first_job]['steps']
                self.assertEqual(steps[0]['name'], 'Verify reviewed commit')
                self.assertEqual(steps[0]['env']['EXPECTED_SHA'], '${{ inputs.expected_sha }}')
                self.assertNotIn('if', steps[0])
                for job in workflow['jobs'].values():
                    for step in job['steps']:
                        if step.get('uses', '').startswith('actions/checkout@'):
                            if step.get('name') == 'Checkout selected dependency revision':
                                self.assertEqual(step['with']['ref'], '${{ inputs.dependency_sha || github.sha }}')
                                self.assertIn(step['with']['path'], ('ci-out/linux-source', 'ci-out/lvpm-source'))
                                self.assertEqual(step['with']['persist-credentials'], 'false')
                            else:
                                self.assertEqual(step['with']['ref'], '${{ github.sha }}')
                if first_job == 'gate':
                    self.assertEqual(workflow['jobs']['build-and-push']['needs'], 'gate')
                    self.assertEqual(workflow['jobs']['build-and-push']['if'], "needs.gate.outputs.build == 'true'")

    def test_selected_dependency_revision_requires_full_committed_ancestor(self):
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        previous = subprocess.check_output(['git', 'rev-parse', 'HEAD^'], cwd=ROOT, text=True).strip()
        for filename, job in (("build-labview-lvpm-image.yml", "candidate"),
                              ("build-labview-linux-image.yml", "gate")):
            workflow = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())
            step = next(step for step in workflow['jobs'][job]['steps']
                        if step.get('name') == 'Validate selected dependency revision')
            for revision, expected in ((head, 0), (previous, 0), ('f' * 40, 1), ('abc123', 1), ('$(id)', 1)):
                with self.subTest(workflow=filename, revision=revision):
                    result = subprocess.run(['bash', '-e', '-c', step['run']], cwd=ROOT,
                        env={**os.environ, 'DEPENDENCY_SHA': revision, 'GITHUB_SHA': head},
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, expected == 0, result.stderr)

    def test_actual_linux_sha_guard_accepts_matching_and_legacy_empty_inputs(self):
        for filename, job in (("build-labview-lvpm-image.yml", "candidate"),
                              ("build-labview-linux-image.yml", "gate")):
            workflow = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())
            script = workflow['jobs'][job]['steps'][0]['run']
            for expected in ('', 'a' * 40, 'A' * 40, 'aA' * 20):
                with self.subTest(workflow=filename, expected=expected):
                    result = subprocess.run(['bash', '-e', '-c', script],
                                            env={**os.environ, 'EXPECTED_SHA': expected, 'GITHUB_SHA': 'a' * 40},
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_linux_sha_guard_rejects_race_invalid_and_shell_text(self):
        for filename, job in (("build-labview-lvpm-image.yml", "candidate"),
                              ("build-labview-linux-image.yml", "gate")):
            workflow = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())
            script = workflow['jobs'][job]['steps'][0]['run']
            for expected in ('b' * 40, 'B' * 40, 'G' * 40, 'a' * 39, 'a' * 41, '$(echo injected)', 'a' * 40 + '\n'):
                with self.subTest(workflow=filename, expected=expected):
                    result = subprocess.run(['bash', '-e', '-c', script],
                                            env={**os.environ, 'EXPECTED_SHA': expected, 'GITHUB_SHA': 'a' * 40},
                                            capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('injected', result.stdout)
                    if expected.lower() == 'b' * 40:
                        self.assertIn('Reviewed HEAD moved', result.stdout)
                        self.assertIn('Click Update container image again', result.stdout)
                    else:
                        self.assertIn('Invalid reviewed commit SHA', result.stdout)

    def test_production_checks_trigger_for_reviewed_sha_workflows_and_worker_tests(self):
        workflow = yaml.load((ROOT / '.github/workflows/lvpm-checks.yml').read_text(), Loader=yaml.BaseLoader)
        for event in ('pull_request', 'push'):
            for path in ('.github/workflows/build-labview-lvpm-image.yml',
                         '.github/workflows/build-labview-linux-image.yml',
                         '.github/labview/test-lvpm-worker.py'):
                with self.subTest(event=event, path=path):
                    self.assertTrue(any(fnmatch.fnmatchcase(path, pattern)
                                        for pattern in workflow['on'][event]['paths']))
        commands = '\n'.join(step.get('run', '') for step in workflow['jobs']['offline-contracts']['steps'])
        self.assertIn('python .github/labview/test-lvpm-worker.py', commands)
        self.assertIn('node .github/labview/test-lvpm-ux.mjs', commands)

    def test_all_owned_workflows_parse_and_embedded_scripts_compile(self):
        for name in ("build-labview-lvpm-image.yml", "build-labview-linux-image.yml", "masscompile-linux-container.yml", "run-vi-analyzer-linux-container.yml",
                     "vidiff-linux-container.yml", "build-binaries-linux-container.yml", "vi-snapshots-json.yml"):
            with self.subTest(workflow=name):
                data = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
                for job in data["jobs"].values():
                    for step in job.get("steps", []):
                        script = step.get("run", "")
                        if not script:
                            continue
                        subprocess.run(["bash", "-n"], input=script, text=True, check=True, capture_output=True)
                        for fragment in script.split("python3 - <<'PY'\n")[1:]:
                            ast.parse(fragment.split("\nPY")[0])

    def test_linux_activity_resolvers_verify_and_do_not_wait_for_vipm(self):
        for name in ("masscompile-linux-container.yml", "run-vi-analyzer-linux-container.yml",
                     "vidiff-linux-container.yml", "build-binaries-linux-container.yml"):
            with self.subTest(workflow=name):
                data = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
                steps = [step for job in data["jobs"].values() for step in job.get("steps", [])]
                resolver = next(step for step in steps if step.get("name") == "Resolve Linux container image name")
                self.assertIn("GH_TOKEN", resolver["env"])
                self.assertIn("pip install --quiet PyYAML==6.0.3 tomli==2.4.0", resolver["run"])
                self.assertIn("worker.py resolve", resolver["run"])
                self.assertIn('linux-lvpm-*@sha256:*) image="ghcr.io/${owner}/${name}:${selected}"; worker_workflow=""; verified_lvpm=true', resolver["run"])
                self.assertIn('echo "LVCI_VERIFIED_LVPM=$verified_lvpm"', resolver["run"])
                wait = next(step for step in steps if step.get("name") == "Wait for Linux worker image build")
                self.assertIn('if [ "$LABVIEW_CONTAINER_IMAGE" = "nationalinstruments/labview:latest-linux" ]', wait["run"])
                self.assertIn('if [ "$LVCI_VERIFIED_LVPM" = "true" ]', wait["run"])
                self.assertNotIn('if [ -z "$LINUX_WORKER_WORKFLOW" ]', wait["run"])

    def test_wait_exception_only_skips_ni_base_or_explicitly_verified_lvpm(self):
        for workflow in ("masscompile-linux-container.yml", "run-vi-analyzer-linux-container.yml",
                         "vidiff-linux-container.yml", "build-binaries-linux-container.yml"):
            data = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
            wait = next(step for job in data["jobs"].values() for step in job.get("steps", [])
                        if step.get("name") == "Wait for Linux worker image build")
            script = re.sub(r"\$\{\{[^}]+\}\}", "test-sha", wait["run"])
            for image, marker, workflow_name, expected_wait in (
                    ("nationalinstruments/labview:latest-linux", "false", "", False),
                    ("ghcr.io/example/project:linux-custom", "false", "VIPM", True),
                    ("ghcr.io/example/project:linux-custom", "false", "", True),
                    ("ghcr.io/example/project:linux-lvpm-verified", "true", "", False)):
                with self.subTest(workflow=workflow, image=image, marker=marker), \
                        tempfile.TemporaryDirectory() as temporary:
                    repo = Path(temporary)
                    helper = repo / ".github/labview/wait-for-worker-image.sh"
                    helper.parent.mkdir(parents=True)
                    helper.write_text('printf called > "$WAIT_LOG"\n')
                    log = repo / "wait.log"
                    result = subprocess.run(["bash", "-e", "-c", script], cwd=repo,
                        env={**os.environ, "LABVIEW_CONTAINER_IMAGE": image, "LVCI_VERIFIED_LVPM": marker,
                             "LINUX_WORKER_WORKFLOW": workflow_name, "WAIT_LOG": str(log)},
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(log.exists(), expected_wait)

    def test_snapshot_lvpm_pull_failure_never_falls_back(self):
        data = yaml.safe_load((ROOT / ".github/workflows/vi-snapshots-json.yml").read_text())
        steps = data["jobs"]["export-json"]["steps"]
        resolver = next(step for step in steps if step.get("id") == "img")
        self.assertIn("worker.py verify", resolver["run"])
        self.assertIn("worker.py resolve", resolver["run"])
        pull = next(step for step in steps if step.get("id") == "pull")
        strict_check = pull["run"].index('if [ "$LVPM_CANDIDATE" = "true" ]')
        fallback = pull["run"].index('if [ "$IMG" != "$FALLBACK" ]')
        self.assertLess(strict_check, fallback)
        self.assertIn("exit 1", pull["run"][strict_check:fallback])

    def test_snapshot_bare_and_full_lvpm_overrides_are_normalized_and_never_fall_back(self):
        data = yaml.safe_load((ROOT / ".github/workflows/vi-snapshots-json.yml").read_text())
        steps = data["jobs"]["export-json"]["steps"]
        resolver = next(step for step in steps if step.get("id") == "img")["run"]
        pull = next(step for step in steps if step.get("id") == "pull")["run"]
        selection = f'linux-lvpm-{"a" * 64}@sha256:{"b" * 64}'
        canonical = f"ghcr.io/example/custom:{selection}"
        for override in (selection, canonical):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                executables = repo / "bin"
                executables.mkdir()
                python = executables / "python3"
                python.write_text('#!/bin/sh\nif [ "$1" = "-m" ]; then exit 0; fi\n'
                                  'if [ "$2" = "verify" ]; then exit "$VERIFY_EXIT"; fi\n'
                                  'printf "%s\\n" "$RESOLVED_SELECTION"\n')
                python.chmod(0o755)
                docker = executables / "docker"
                docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\nexit 1\n')
                docker.chmod(0o755)
                output = repo / "output"
                log = repo / "docker.log"
                environment = {**os.environ, "PATH": f'{executables}:{os.environ["PATH"]}',
                    "GITHUB_REPOSITORY": "Example/Project", "IMAGE_OVERRIDE": "custom",
                    "TOIMAGES_OVERRIDE": override, "GITHUB_OUTPUT": str(output),
                    "VERIFY_EXIT": "0", "RESOLVED_SELECTION": "", "DOCKER_LOG": str(log)}
                result = subprocess.run(["bash", "-e", "-c", resolver], cwd=repo, env=environment,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual(values["image"], canonical)
                self.assertEqual(values["lvpm"], "true")
                result = subprocess.run(["bash", "-e", "-c", pull], cwd=repo,
                    env={**environment, "IMG": values["image"], "FALLBACK": values["fallback"],
                         "LVPM_CANDIDATE": values["lvpm"]}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(log.read_text().splitlines(), [f"pull {canonical}"])
                output.unlink()
                result = subprocess.run(["bash", "-e", "-c", resolver], cwd=repo,
                    env={**environment, "VERIFY_EXIT": "1"}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())
    def test_dockerfile_is_pinned_external_source_without_vipm(self):
        text = (ROOT / ".github/docker/labview-ci-lvpm.Dockerfile").read_text()
        self.assertIn("cargo build --release --locked --package lvpm", text)
        self.assertIn(f'test "$(git rev-parse HEAD)" = {worker.TOOL_REVISION}', text)
        self.assertIn("LICENSE-MIT", text)
        self.assertIn("LICENSE-APACHE", text)
        self.assertIn("python3-yaml python3-tomli", text)
        self.assertNotIn("vipm", text.lower())


if __name__ == "__main__":
    unittest.main()
