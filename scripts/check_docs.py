"""Small dependency-free checks for repository documentation."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if ".git" not in path.parts and "node_modules" not in path.parts
    )


def main() -> int:
    errors: list[str] = []

    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT)

        if text and not text.endswith("\n"):
            errors.append(f"{relative}: missing final newline")

        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.endswith((" ", "\t")):
                errors.append(f"{relative}:{line_number}: trailing whitespace")

        for match in MARKDOWN_LINK.finditer(text):
            target = match.group(1).strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue

            target = unquote(target.split("#", maxsplit=1)[0])
            if not target:
                continue

            linked_path = (path.parent / target).resolve()
            try:
                linked_path.relative_to(ROOT)
            except ValueError:
                errors.append(f"{relative}: link escapes repository: {target}")
                continue

            if not linked_path.exists():
                errors.append(f"{relative}: broken local link: {target}")

    if errors:
        print("Documentation checks failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Documentation checks passed ({len(markdown_files())} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

