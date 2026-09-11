#!/usr/bin/env python3
"""Write a GitHub PR body or issue comment, then verify the exact resource."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


class WriteError(Exception):
    """A write could not be verified; callers must not retry automatically."""


def normalize_body(body: str) -> str:
    return body.replace("\r\n", "\n").rstrip("\n")


def api(method: str, endpoint: str, body: str | None = None) -> dict:
    command = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
    payload = None
    if body is not None:
        command.extend(["--input", "-"])
        payload = json.dumps({"body": body}, ensure_ascii=False)
    try:
        result = subprocess.run(
            command, input=payload, capture_output=True, text=True,
            encoding="utf-8", errors="strict", check=False,
        )
    except (OSError, UnicodeError) as exc:
        raise WriteError(f"GitHub {method} invocation failed") from exc
    if result.returncode != 0:
        raise WriteError(f"GitHub {method} returned exit code {result.returncode}")
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise WriteError(f"GitHub {method} returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise WriteError(f"GitHub {method} returned a non-object response")
    return response


def verify_fields(response: dict, expected: dict) -> None:
    for field, value in expected.items():
        if type(response.get(field)) is not type(value) or response[field] != value:
            raise WriteError(f"GitHub response has an unexpected {field}")
    if not isinstance(response.get("body"), str):
        raise WriteError("GitHub response is missing a string body")


def verified_write(repo: str, number: int, kind: str, body: str) -> str:
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo):
        raise WriteError("Repository must use owner/repo format")
    if type(number) is not int or number <= 0:
        raise WriteError("Target number must be a positive integer")
    if kind not in ("pr-body", "pr-comment", "issue-comment"):
        raise WriteError("Unsupported write kind")
    if not isinstance(body, str) or not body.strip():
        raise WriteError("Body must be nonempty text")
    is_pr = kind.startswith("pr-")
    target = f"https://github.com/{repo}/{'pull' if is_pr else 'issues'}/{number}"
    prefix = f"repos/{repo}"
    # Emit the known target before mutation: a failed POST may still create a comment.
    print(f"Target: {target}; do not retry automatically if verification fails", file=sys.stderr)
    if kind == "pr-body":
        endpoint = f"{prefix}/pulls/{number}"
        expected = {"number": number, "url": f"https://api.github.com/{endpoint}", "html_url": target}
        written = api("PATCH", endpoint, body)
        verify_fields(written, expected)
    else:
        # Resolve PR-vs-Issue identity before a comment mutation.
        endpoint = f"{prefix}/{'pulls' if is_pr else 'issues'}/{number}"
        identity = api("GET", endpoint)
        verify_fields(identity, {"number": number, "url": f"https://api.github.com/{endpoint}", "html_url": target})
        written = api("POST", f"{prefix}/issues/{number}/comments", body)
        comment_id = written.get("id")
        if type(comment_id) is not int or comment_id <= 0:
            raise WriteError("GitHub comment response has an invalid id")
        endpoint = f"{prefix}/issues/comments/{comment_id}"
        target = f"{target}#issuecomment-{comment_id}"
        print(f"Created comment id: {comment_id}; expected URL: {target}", file=sys.stderr)
        expected = {
            "id": comment_id, "url": f"https://api.github.com/{endpoint}",
            "html_url": target, "issue_url": f"https://api.github.com/{prefix}/issues/{number}",
        }
        verify_fields(written, expected)
    actual = api("GET", endpoint)
    verify_fields(actual, expected)
    if normalize_body(actual["body"]) != normalize_body(body):
        raise WriteError("GitHub readback body does not match the requested body")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True, type=int)
    parser.add_argument("--kind", required=True, choices=("pr-body", "pr-comment", "issue-comment"))
    parser.add_argument("--body-file", required=True)
    args = parser.parse_args(argv)
    try:
        body = Path(args.body_file).resolve(strict=True).read_text(encoding="utf-8-sig")
        target = verified_write(args.repo, args.number, args.kind, body)
    except (WriteError, OSError, UnicodeError, ValueError) as exc:
        # Never expose request bodies or gh stderr, which may contain private data.
        message = str(exc) if isinstance(exc, WriteError) else "Cannot read body file as UTF-8 text"
        print(f"Write verification failed: {message}", file=sys.stderr)
        return 2
    print(f"Verified: {target}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Write interrupted; inspect the target before any retry", file=sys.stderr)
        sys.exit(2)
