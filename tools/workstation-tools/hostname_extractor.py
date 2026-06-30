#!/usr/bin/env python3
"""
hostname_extractor.py

Purpose:
    Extract possible hostnames from copied text, logs, ticket titles, or notes.

Created by:
    Avraham Makovsky

License:
    MIT

Why it exists:
    In support work, useful hostnames are often buried inside copied text.
    This tool turns that text into a clean, deduplicated host list.

Input options:
    - text file
    - stdin
    - temporary Notepad window on Windows

Examples:
    python hostname_extractor.py notes.txt
    python hostname_extractor.py --prefix LAB-PC-
    type notes.txt | python hostname_extractor.py
    python hostname_extractor.py --notepad

Dependencies:
    Python standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Pattern


DEFAULT_HOSTNAME_PATTERN = r"""
\b
(?=
    [A-Za-z0-9-]{3,63}\b
)
(?=
    [A-Za-z0-9-]*[A-Za-z]
)
(?=
    [A-Za-z0-9-]*[0-9-]
)
[A-Za-z0-9]
[A-Za-z0-9-]{1,61}
[A-Za-z0-9]
\b
"""


def build_pattern(prefix: str | None, regex: str | None) -> Pattern[str]:
    """Return the hostname matching pattern requested by the user."""
    if regex:
        return re.compile(regex, re.IGNORECASE)

    if prefix:
        escaped_prefix = re.escape(prefix)
        return re.compile(rf"\b{escaped_prefix}[A-Za-z0-9-]*[A-Za-z0-9]\b", re.IGNORECASE)

    return re.compile(DEFAULT_HOSTNAME_PATTERN, re.IGNORECASE | re.VERBOSE)


def read_from_notepad() -> str:
    """Open Notepad for pasted input and return the saved text."""
    if os.name != "nt":
        raise RuntimeError("--notepad is only supported on Windows.")

    temp_path = None

    try:
        fd, temp_path = tempfile.mkstemp(prefix="hostname_input_", suffix=".txt", text=True)
        os.close(fd)

        Path(temp_path).write_text(
            "# Paste text here, then save and close Notepad.\n"
            "# Example:\n"
            "LAB-PC-001 maintenance completed\n"
            "TEST-HOST-03 needs follow-up\n",
            encoding="utf-8",
        )

        subprocess.run(["notepad.exe", temp_path], check=False)

        return Path(temp_path).read_text(encoding="utf-8", errors="replace")

    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass


def read_input(args: argparse.Namespace) -> str:
    """Read input from Notepad, file, stdin, or interactive paste."""
    if args.notepad:
        return read_from_notepad()

    if args.file:
        return Path(args.file).read_text(encoding="utf-8", errors="replace")

    if not sys.stdin.isatty():
        return sys.stdin.read()

    print("Paste text below. Finish with Ctrl+Z then Enter on Windows, or Ctrl+D on Linux/macOS.")
    return sys.stdin.read()


def normalize_hostname(value: str, uppercase: bool) -> str:
    """Trim punctuation and normalize case."""
    value = value.strip().strip(".,;:()[]{}<>\"'")
    return value.upper() if uppercase else value.lower()


def extract_hostnames(text: str, pattern: Pattern[str], uppercase: bool) -> List[str]:
    """Extract, normalize, deduplicate, and sort hostname candidates."""
    found = []
    seen = set()

    for match in pattern.findall(text):
        raw = next((part for part in match if part), "") if isinstance(match, tuple) else match
        hostname = normalize_hostname(raw, uppercase=uppercase)

        if hostname and hostname not in seen:
            seen.add(hostname)
            found.append(hostname)

    return sorted(found)


def write_output(hostnames: Iterable[str], output_path: str | None) -> None:
    """Print hostnames or save them to a text file."""
    lines = list(hostnames)
    text = "\n".join(lines)

    if text:
        text += "\n"

    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
        print(f"Saved {len(lines)} hostnames to: {output_path}")
        return

    if not lines:
        print("No hostnames found.")
        return

    print("\nExtracted hostnames:")
    for hostname in lines:
        print(hostname)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract possible hostnames from copied text, logs, ticket titles, or notes."
    )

    parser.add_argument("file", nargs="?", help="Optional text file to read.")
    parser.add_argument("--prefix", help="Only match hostnames that start with this prefix, for example LAB-PC-.")
    parser.add_argument("--regex", help="Custom regular expression for hostname matching.")
    parser.add_argument("--output", "-o", help="Optional output text file for extracted hostnames.")
    parser.add_argument("--uppercase", action="store_true", help="Print hostnames in uppercase.")
    parser.add_argument("--notepad", action="store_true", help="Open Notepad for pasted input. Windows only.")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        text = read_input(args)
        pattern = build_pattern(prefix=args.prefix, regex=args.regex)
        hostnames = extract_hostnames(text, pattern=pattern, uppercase=args.uppercase)
        write_output(hostnames, output_path=args.output)
        return 0

    except KeyboardInterrupt:
        print("\nCanceled.")
        return 130

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
