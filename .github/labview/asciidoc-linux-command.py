#!/usr/bin/env python3
"""Bridge AsciiDoc 1.12.1's `cmd /c` calls to its native Linux Ruby tools.

The package's Send Ruby Command.vi uses this Windows prefix on Linux too.
This is deliberately not a Windows shell: only the package's native rendering
commands and its gem discovery/update checks are supported. Arguments, output and exit
status belong to the real executable; no shell evaluation or success fallback.
"""

import os
import sys


def native_command(arguments):
    if len(arguments) < 2 or arguments[0].lower() != "/c":
        raise ValueError("Expected AsciiDoc's cmd /c invocation")
    command = arguments[1:]
    if command[0] in ("asciidoctor", "asciidoctor-pdf"):
        return command
    if command == ["gem", "outdated"]:
        # The hook otherwise upgrades the pinned toolchain (and opens a sudo
        # dialog), even when all required tools passed their native checks.
        # Use RubyGems' own source controls: no remote update discovery during
        # an immutable worker build. Normal `gem outdated` remains unchanged.
        return ["gem", "outdated", "--local", "--clear-sources"]
    if command in (["gem", "which", "asciidoctor-kroki"],
                   ["gem", "which", "asciimath"]):
        return command
    raise ValueError("Unsupported command in AsciiDoc Linux compatibility wrapper")


def main():
    try:
        command = native_command(sys.argv[1:])
        if command[:2] == ["gem", "outdated"]:
            print("Pinned CI toolchain: remote Ruby upgrade discovery is disabled.", file=sys.stderr)
        os.execvp(command[0], command)
    except (ValueError, OSError) as error:
        print(f"AsciiDoc Linux command failed: {error}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
