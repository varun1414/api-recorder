"""The ``apirecorder`` command line interface.

Three commands:

``inspect``  what was recorded, in order
``report``   a behaviour report: observed shapes, plus what the recording misses
``diff``     a baseline against a fresh recording; exits 1 on a breaking change
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .analyze import analyse
from .diff import diff_analyses
from .model import load_captures
from .report import (
    render_diff,
    render_diff_markdown,
    render_inspect,
    render_report,
)


def _load(path: str) -> List:
    captures = load_captures(Path(path))
    if not captures:
        raise SystemExit("no recordings found in %s" % path)
    return captures


def _emit(text: str, destination: Optional[str]) -> None:
    if destination:
        out = Path(destination)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        print("wrote %s" % out)
    else:
        print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apirecorder",
        description="Record what a third-party API actually does, then diff it "
        "over time.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_inspect = sub.add_parser(
        "inspect", help="list the recorded calls in order"
    )
    p_inspect.add_argument("recordings", help="a capture file or a directory of them")

    p_report = sub.add_parser(
        "report", help="write a behaviour report (observed shapes + coverage gaps)"
    )
    p_report.add_argument("recordings", help="a capture file or a directory of them")
    p_report.add_argument(
        "-o", "--output", help="write Markdown here instead of stdout"
    )
    p_report.add_argument(
        "--title", default="API behaviour", help="report heading"
    )

    p_diff = sub.add_parser(
        "diff",
        help="compare a baseline recording against a new one; exit 1 on a "
        "breaking change",
    )
    p_diff.add_argument("baseline", help="the committed baseline recordings")
    p_diff.add_argument("current", help="the recordings from this run")
    p_diff.add_argument(
        "-o", "--output", help="write the diff here instead of stdout"
    )
    p_diff.add_argument(
        "--format",
        choices=("text", "markdown"),
        default="text",
        help="output format (default: text)",
    )
    p_diff.add_argument(
        "--strict",
        action="store_true",
        help="also exit 1 when the only changes are additive",
    )
    p_diff.add_argument(
        "--no-info",
        action="store_true",
        help="hide info-level changes in text output",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 2

    if args.command == "inspect":
        print(render_inspect(_load(args.recordings)))
        return 0

    if args.command == "report":
        text = render_report(_load(args.recordings), title=args.title)
        _emit(text, args.output)
        return 0

    if args.command == "diff":
        baseline = analyse(_load(args.baseline))
        current = analyse(_load(args.current))
        result = diff_analyses(
            baseline,
            current,
            baseline_label=args.baseline,
            current_label=args.current,
        )
        if args.format == "markdown":
            text = render_diff_markdown(result)
        else:
            text = render_diff(result, show_info=not args.no_info)
        _emit(text, args.output)
        return result.exit_code(strict=args.strict)

    parser.print_help()  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
