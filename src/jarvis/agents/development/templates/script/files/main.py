"""Command-line entry point. Run `python main.py --help` for the commands."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import core

log = logging.getLogger("tool")


def cmd_stats(args: argparse.Namespace) -> int:
    report = core.summarize(core.scan(args.path, include_hidden=args.hidden), top=args.top)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"{report['files']} files, {core.human_size(report['total_size'])}")
    print("\nBy type:")
    for row in report["by_extension"][: args.top * 2]:
        print(f"  {row['extension']:<10} {row['files']:>6}  {core.human_size(row['size']):>10}")
    print("\nLargest:")
    for row in report["largest"]:
        print(f"  {core.human_size(row['size']):>10}  {row['path']}")
    return 0


def cmd_dupes(args: argparse.Namespace) -> int:
    groups = core.find_duplicates(core.scan(args.path, include_hidden=args.hidden))
    if args.json:
        print(json.dumps([[str(p) for p in g] for g in groups], ensure_ascii=False, indent=2))
        return 0
    if not groups:
        print("No duplicate files found.")
        return 0
    for i, group in enumerate(groups, 1):
        print(f"Group {i}:")
        for path in group:
            print(f"  {path}")
    return 0


def cmd_words(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding=args.encoding, errors="replace")
    words = core.top_words(text, args.top)
    if args.json:
        print(json.dumps(words, ensure_ascii=False))
        return 0
    for word, count in words:
        print(f"{count:>6}  {word}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Folder statistics, duplicate files and word counts.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    stats = sub.add_parser("stats", help="Summarize the files in a folder")
    stats.add_argument("path", type=Path)
    stats.add_argument("--top", type=int, default=5)
    stats.set_defaults(func=cmd_stats)

    dupes = sub.add_parser("dupes", help="Find duplicate files in a folder")
    dupes.add_argument("path", type=Path)
    dupes.set_defaults(func=cmd_dupes)

    for p in (stats, dupes):
        p.add_argument("--hidden", action="store_true", help="Include hidden files and folders")
        p.add_argument("--json", action="store_true", help="Print JSON")

    words = sub.add_parser("words", help="The most common words in a text file")
    words.add_argument("file", type=Path)
    words.add_argument("--top", type=int, default=10)
    words.add_argument("--encoding", default="utf-8")
    words.add_argument("--json", action="store_true", help="Print JSON")
    words.set_defaults(func=cmd_words)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        log.debug("failed", exc_info=True)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
