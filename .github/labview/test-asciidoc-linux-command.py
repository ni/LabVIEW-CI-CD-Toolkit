#!/usr/bin/env python3
"""Exercise the Linux bridge using subprocess output, arguments and failures."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BRIDGE = Path(__file__).with_name("asciidoc-linux-command.py")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bin = Path(self.temporary.name)
        self.environment = {**os.environ, "PATH": str(self.bin)}
        self.bridge = self.bin / "cmd"
        self.bridge.write_bytes(BRIDGE.read_bytes())
        (self.bin / "gemrc").write_text(":sources: []\n")
        for tool in ("asciidoctor", "asciidoctor-pdf", "gem"):
            path = self.bin / tool
            path.write_text(f"#!{sys.executable}\n"
                            "import json, sys\n"
                            "print(json.dumps(sys.argv[1:]))\n"
                            "print('native diagnostic', file=sys.stderr)\n"
                            "sys.exit(17 if '--fail' in sys.argv else 0)\n")
            path.chmod(0o755)

    def invoke(self, *arguments):
        return subprocess.run([sys.executable, str(self.bridge), *arguments],
                              env=self.environment, capture_output=True, text=True)

    def test_observed_package_checks_execute_native_tools(self):
        for command in (("asciidoctor", "--version"), ("asciidoctor-pdf", "--version"),
                        ("gem", "which", "asciidoctor-kroki"), ("gem", "which", "asciimath")):
            with self.subTest(command=command):
                result = self.invoke("/c", *command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), list(command[1:]))
                self.assertEqual(result.stderr, "native diagnostic\n")

    def test_optional_update_query_cannot_upgrade_the_pinned_toolchain(self):
        result = self.invoke("/c", "gem", "outdated")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["outdated", "--config-file", str((self.bin / "gemrc").resolve())])
        self.assertIn("remote Ruby upgrade discovery is disabled", result.stderr)
        self.assertIn("native diagnostic", result.stderr)

    def test_renderer_arguments_are_preserved_without_shell_evaluation(self):
        marker = self.bin / "must-not-exist"
        arguments = ["-o", "report with spaces.pdf", f"$(touch {marker}).adoc"]
        result = self.invoke("/c", "asciidoctor-pdf", *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)
        self.assertFalse(marker.exists())

    def test_native_failure_is_not_reported_as_success(self):
        result = self.invoke("/c", "asciidoctor", "--fail")
        self.assertEqual(result.returncode, 17)
        self.assertEqual(result.stderr, "native diagnostic\n")

    def test_missing_native_tool_fails(self):
        (self.bin / "asciidoctor").unlink()
        result = self.invoke("/c", "asciidoctor", "--version")
        self.assertEqual(result.returncode, 127)
        self.assertIn("No such file", result.stderr)

    def test_unsupported_commands_and_windows_shell_options_fail(self):
        for arguments in ((), ("/c",), ("/k", "asciidoctor"), ("/c", "sh", "-c", "true"),
                          ("/c", "gem", "install", "asciidoctor"),
                          ("/c", "gem", "which", "unknown"),
                          ("/c", "asciidoctor --version")):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 127)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
