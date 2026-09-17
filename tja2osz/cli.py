"""Command line interface."""
from __future__ import annotations

import argparse
import codecs
import sys
from pathlib import Path

from . import __version__, lazer, osu
from .convert import Options, convert_file
from .tja import COURSE_ALIASES


def _range(lo: float, hi: float):
    def parse(text: str) -> float:
        value = float(text)
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tja2osz",
        description="Convert TJA charts to osu!taiko beatmaps (.osz).",
        epilog="Exit status: 0 = all files converted, 1 = bad arguments / no input, 2 = some files failed.")
    p.add_argument("inputs", nargs="+", help=".tja files or folders (searched recursively)")
    p.add_argument("-o", "--output", type=Path, help="output folder (default: next to each .tja)")
    p.add_argument("--target", choices=[osu.LAZER, osu.STABLE], default=osu.LAZER,
                   help="lazer: exact fractional times (default); stable: whole milliseconds for osu!stable")
    p.add_argument("--branch", choices=["N", "E", "M"], default="M",
                   help="branch route to convert: N(ormal), E(xpert), M(aster) (default: M)")
    p.add_argument("--courses", help="comma-separated courses to convert: easy,normal,hard,oni,edit,tower,dan")
    p.add_argument("--no-osz", action="store_true", help="write a beatmap folder instead of an .osz")
    p.add_argument("--folder", action="store_true", help="write a beatmap folder in addition to the .osz")

    meta = p.add_argument_group("metadata")
    meta.add_argument("--title", help="romanised (ASCII) title (default: TITLEEN, or TITLE if ASCII)")
    meta.add_argument("--artist", help="romanised (ASCII) artist (default: SUBTITLEEN, or SUBTITLE if ASCII)")
    meta.add_argument("--artist-unicode", help="artist in its original language (default: SUBTITLEJA or SUBTITLE)")
    meta.add_argument("--creator", help="beatmap creator (default: MAKER)")
    meta.add_argument("--source", help="source (default: taken from a 'from ...' SUBTITLE)")

    diff = p.add_argument_group("difficulty")
    diff.add_argument("--hp", type=_range(0, 10), help="HP drain rate for every difficulty")
    diff.add_argument("--od", type=_range(0, 10), help="overall difficulty for every difficulty")
    diff.add_argument("--slider-multiplier", type=_range(lazer.MIN_SLIDER_MULTIPLIER, lazer.MAX_SLIDER_MULTIPLIER),
                      default=1.4, help="base scroll speed (default: 1.4)")
    diff.add_argument("--tick-rate", type=_range(lazer.MIN_TICK_RATE, lazer.MAX_TICK_RATE), default=4.0,
                      help="slider tick rate (default: 4)")

    checks = p.add_argument_group("checks")
    checks.add_argument("--strict", action="store_true", help="treat warnings as errors")
    checks.add_argument("--force", action="store_true", help="write output even when there are errors")
    checks.add_argument("--encoding", help="force the TJA text encoding (e.g. cp932, utf-8)")
    checks.add_argument("-v", "--verbose", action="store_true", help="also show informational messages")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _find_inputs(inputs: list[str]) -> tuple[list[Path], list[str]]:
    files, missing = [], []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            files += sorted(q for q in path.rglob("*") if q.is_file() and q.suffix.lower() == ".tja")
        elif path.is_file():
            files.append(path)
        else:
            missing.append(raw)
    return files, missing


def main(argv: list[str] | None = None) -> int:
    # Never crash on consoles that cannot show a character (e.g. cp932 with emoji file names).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    courses = None
    if args.courses:
        courses = set()
        for name in args.courses.split(","):
            course = COURSE_ALIASES.get(name.strip().lower())
            if course is None:
                parser.error(f"unknown course '{name.strip()}'")
            courses.add(course)
    if args.encoding:
        try:
            codecs.lookup(args.encoding)
        except LookupError:
            parser.error(f"unknown encoding '{args.encoding}'")

    options = Options(
        target=args.target, branch=args.branch, courses=courses,
        creator=args.creator, title=args.title, artist=args.artist, artist_unicode=args.artist_unicode,
        source=args.source, hp=args.hp, od=args.od,
        slider_multiplier=args.slider_multiplier, tick_rate=args.tick_rate,
        encoding=args.encoding, strict=args.strict, force=args.force, output=args.output,
        write_osz=not args.no_osz, write_folder=args.folder or args.no_osz,
    )

    files, missing = _find_inputs(args.inputs)
    for raw in missing:
        print(f"error: not found: {raw}", file=sys.stderr)
    if not files:
        print("error: no .tja files to convert", file=sys.stderr)
        return 1

    converted = 0
    for path in files:
        print(f"* {path}")
        try:
            result = convert_file(path, options)
        except OSError as e:
            print(f"  error   {e}")
            continue
        for line in result.diagnostics.format(args.verbose):
            print(line)
        for d in result.difficulties:
            end = f", roll/balloon ends <= {d.max_end_error:.3f} ms" if d.max_end_error else ""
            print(f"  -> [{d.version}] {d.objects} objects, max time error {d.max_note_error:.6f} ms{end}")
        if result.converted:
            converted += 1
            for output in result.outputs:
                print(f"  wrote {output}")
        else:
            print("  skipped: fix the errors above (or use --force)")

    print(f"Done: {converted}/{len(files)} converted.")
    return 0 if converted == len(files) and not missing else 2
