"""Fail CI when tracked files appear to contain committed credentials."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "generic model API key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{30,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
DEEPSEEK_ASSIGNMENT = re.compile(
    r"^[ \t]*DEEPSEEK_API_KEY[ \t]*=[ \t]*([^\r\n#]*)", re.MULTILINE
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item for item in result.stdout.decode().split("\0") if item]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{path.relative_to(ROOT)}: {label}")
        for match in DEEPSEEK_ASSIGNMENT.finditer(content):
            value = match.group(1).strip().strip("\"'")
            if value and not value.startswith(("$", "[", "<")):
                findings.append(
                    f"{path.relative_to(ROOT)}: non-empty DeepSeek env assignment"
                )

    if findings:
        print("Potential credentials found in tracked files (values hidden):")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("Secret scan passed for tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
