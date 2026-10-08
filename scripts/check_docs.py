#!/usr/bin/env python3
"""Check relative Markdown links and fences; no application imports or network."""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


def check_file(path: Path) -> tuple[int, list[str]]:
    text = path.read_text(encoding="utf-8")
    outside: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            outside.append(line)
    errors = [f"{path}: unclosed code fence"] if fence else []
    count = 0
    for match in re.finditer(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", "\n".join(outside)):
        target = match.group(1).strip()
        if target.startswith("<"):
            if ">" not in target:
                errors.append(f"{path}: malformed angle-bracket link")
                continue
            target = target[1:target.index(">")]
        else:
            target = target.split(' "', 1)[0]
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        count += 1
        resolved = path.parent / unquote(parsed.path)
        if not resolved.exists():
            errors.append(f"{path}: missing relative target {target}")
    return count, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("docs/handbook")])
    args = parser.parse_args()
    files: set[Path] = set()
    errors: list[str] = []
    for path in args.paths:
        if path.is_dir():
            files.update(path.rglob("*.md"))
        elif path.is_file():
            files.add(path)
        else:
            errors.append(f"missing input {path}")
    links = 0
    for path in sorted(files):
        count, findings = check_file(path)
        links += count
        errors.extend(findings)
    for finding in errors:
        print(finding)
    print(f"Checked {len(files)} files, {links} relative links; {len(errors)} errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
