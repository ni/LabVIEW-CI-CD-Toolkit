#!/usr/bin/env python3

import json
import os
import re
import sys
from pathlib import Path


HEX40 = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> None:
    raise ValueError(message)


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if value == "":
        fail(f"Missing required environment variable: {name}")
    return value


def normalize_sha(value: object, *, source: str) -> str:
    text = str(value or "").strip().lower()
    if not HEX40.match(text):
        fail(f"Invalid {source}: {text}")
    return text


def normalize_pr(value: object, *, source: str) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text == "":
        return ""
    if not text.isdigit() or int(text) <= 0:
        fail(f"Invalid {source}: {text}")
    return str(int(text))


def normalize_platform(value: object, *, source: str) -> str:
    text = str(value or "").strip().lower()
    if text not in {"windows", "linux"}:
        fail(f"Invalid {source}: {text}")
    return text


def normalize_outcome(value: object, *, source: str) -> str:
    text = str(value or "").strip().lower()
    if text not in {"success", "failure"}:
        fail(f"Invalid {source}: {text}")
    return text


def normalize_run_conclusion(value: object, *, source: str) -> str:
    text = str(value or "").strip().lower()
    if text not in {"", "success", "failure", "cancelled", "skipped", "timed_out", "action_required", "neutral", "stale"}:
        fail(f"Invalid {source}: {text}")
    return text


def parse_metadata(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail("Metadata root must be a JSON object")
    return data


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate-vidiff-deploy-metadata.py <vidiff-meta.json>", file=sys.stderr)
        return 2

    metadata_path = Path(sys.argv[1])
    if not metadata_path.is_file():
        fail(f"Missing metadata file: {metadata_path}")

    trusted_artifact_name = require_env("TRUSTED_ARTIFACT_NAME")
    trusted_head_sha = normalize_sha(require_env("TRUSTED_HEAD_SHA"), source="TRUSTED_HEAD_SHA")
    trusted_pr_number = normalize_pr(os.environ.get("TRUSTED_PR_NUMBER", ""), source="TRUSTED_PR_NUMBER")
    trusted_platform = normalize_platform(require_env("TRUSTED_PLATFORM"), source="TRUSTED_PLATFORM")
    trusted_run_conclusion = normalize_run_conclusion(
        os.environ.get("TRUSTED_RUN_CONCLUSION", ""),
        source="TRUSTED_RUN_CONCLUSION",
    )

    expected_artifact_name = f"vidiff-{trusted_platform}-{trusted_head_sha}"
    if trusted_artifact_name != expected_artifact_name:
        fail(
            f"Artifact name mismatch: expected {expected_artifact_name}, got {trusted_artifact_name}"
        )

    metadata = parse_metadata(metadata_path)
    metadata_head_sha = normalize_sha(metadata.get("head_sha", ""), source="metadata.head_sha")
    metadata_pr_number = normalize_pr(metadata.get("pr_number", ""), source="metadata.pr_number")
    metadata_platform = normalize_platform(metadata.get("platform", ""), source="metadata.platform")
    metadata_outcome = normalize_outcome(metadata.get("outcome", ""), source="metadata.outcome")

    if metadata_head_sha != trusted_head_sha:
        fail(f"head_sha mismatch: expected {trusted_head_sha}, got {metadata_head_sha}")
    if metadata_pr_number != trusted_pr_number:
        fail(f"pr_number mismatch: expected {trusted_pr_number}, got {metadata_pr_number}")
    if metadata_platform != trusted_platform:
        fail(f"platform mismatch: expected {trusted_platform}, got {metadata_platform}")

    outcome = "failure" if trusted_run_conclusion and trusted_run_conclusion != "success" else metadata_outcome
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"metadata_outcome={metadata_outcome}\n")
            handle.write(f"outcome={outcome}\n")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
