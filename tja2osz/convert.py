"""Converts one .tja file into osu!taiko difficulties and packages them."""
from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import osu
from .audio import SongPart, build_audio, find_ffmpeg, playable_by_osu
from .diagnostics import ConversionError, Diagnostics
from .timeline import Hit, Roll, Swell
from .tja import ChartSource, DanSong, NotationParser, decode_text, parse_real, split_charts
from .verify import verify

COURSE_ORDER = ["easy", "normal", "hard", "oni", "edit", "tower", "dan"]

VERSION_NAMES = {
    "easy": "Kantan",
    "normal": "Futsuu",
    "hard": "Muzukashii",
    "oni": "Oni",
    "edit": "Inner Oni",
    "tower": "Tower",
    "dan": "Dan",
}

# (HP, OD) within the osu!taiko ranking criteria for each difficulty
DEFAULT_HP_OD = {
    "easy": (8, 3),
    "normal": (7, 4),
    "hard": (6, 5),
    "oni": (5, 5),
    "edit": (5, 6),
    "tower": (5, 5),
    "dan": (5, 5),
}

_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class Options:
    target: str = osu.LAZER
    branch: str = "M"
    courses: set[str] | None = None
    creator: str | None = None
    title: str | None = None
    artist: str | None = None
    artist_unicode: str | None = None
    source: str | None = None
    hp: float | None = None
    od: float | None = None
    slider_multiplier: float = 1.4
    tick_rate: float = 4.0
    encoding: str | None = None
    strict: bool = False
    force: bool = False
    output: Path | None = None
    write_osz: bool = True
    write_folder: bool = False


@dataclass
class Difficulty:
    version: str
    filename: str
    text: str
    objects: int
    max_note_error: float
    max_end_error: float


@dataclass
class FileResult:
    path: Path
    diagnostics: Diagnostics
    difficulties: list[Difficulty] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    converted: bool = False


# Metadata

def is_romanised(text: str) -> bool:
    """osu.Game.Beatmaps.MetadataUtils.IsRomanised: printable ASCII only."""
    return all(" " <= c <= "~" for c in text)


def strip_non_romanised(text: str) -> str:
    """MetadataUtils.StripNonRomanisedCharacters, also collapsing the spaces left behind."""
    return " ".join("".join(c for c in text if " " <= c <= "~").split())


def _clean(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _romanised(field_name: str, candidates: list[str | None], unicode_value: str, hint: str, diag) -> str:
    """Pick the first candidate that osu! accepts in a romanised (Title/Artist) field.

    Without one, non-ASCII characters are removed the way lazer's editor does. Only when nothing
    is left does the conversion stop.
    """
    for candidate in candidates:
        if candidate and is_romanised(candidate):
            return candidate
    if not unicode_value:
        return ""
    stripped = strip_non_romanised(unicode_value)
    if stripped:
        diag.warning(f"{field_name} must be printable ASCII in osu!; '{unicode_value}' -> '{stripped}'. {hint}")
    else:
        diag.error(f"{field_name} must be printable ASCII in osu!, but '{unicode_value}' has no ASCII characters. {hint}")
    return stripped


def _subtitle(value: str) -> str:
    value = value.strip()
    return value[2:].strip() if value[:2] in ("--", "++") else value


def build_metadata(headers: dict[str, str], options: Options, diag) -> osu.Metadata:
    title_unicode = _clean(headers.get("TITLEJA", "") or headers.get("TITLE", ""))
    if not title_unicode:
        diag.error("TITLE is missing")
    title = _romanised("Title", [options.title, _clean(headers.get("TITLEEN", "")), _clean(headers.get("TITLE", ""))],
                       title_unicode, "Add TITLEEN to the TJA or pass --title.", diag)

    subtitle = _subtitle(_clean(headers.get("SUBTITLE", "")))
    subtitle_ja = _subtitle(_clean(headers.get("SUBTITLEJA", "")))
    subtitle_en = _subtitle(_clean(headers.get("SUBTITLEEN", "")))
    subtitle_unicode = subtitle_ja or subtitle
    source = options.source or ""
    # "from <anime/game>" subtitles name the source, anything else is taken as the artist
    subtitle_is_source = (subtitle or subtitle_unicode).lower().startswith("from")
    if subtitle_is_source and not source:
        source = subtitle_unicode[4:].strip(" \u3000:\"'\u300c\u300d")  # also full-width space and corner brackets
    artist_unicode = options.artist_unicode or options.artist or ("" if subtitle_is_source else subtitle_unicode)
    if artist_unicode:
        candidates = [options.artist]
        if not subtitle_is_source:
            candidates += [subtitle_en, subtitle]
        candidates.append(artist_unicode)
        artist = _romanised("Artist", candidates, artist_unicode, "Add SUBTITLEEN to the TJA or pass --artist.", diag)
    else:
        artist = artist_unicode = "Unknown"
        diag.info("the TJA names no artist (SUBTITLE); 'Unknown' is used")

    maker = re.sub(r"<[^>]*>", "", headers.get("MAKER", "")).strip()
    creator = options.creator or maker or "tja2osz"
    if not is_romanised(creator):
        stripped = strip_non_romanised(creator) or "tja2osz"
        diag.warning(f"Creator should be an ASCII osu! username; '{creator}' -> '{stripped}' (use --creator)")
        creator = stripped

    tags = " ".join(t for t in [_clean(headers.get("GENRE", "")).replace(" ", "_"), "tja", "converted"] if t)

    preview = -1
    demo = headers.get("DEMOSTART", "").strip()
    if demo:
        value = parse_real(demo)
        if value is None or value < 0:
            diag.warning(f"DEMOSTART '{demo}' is not a non-negative number; no preview point is set")
        else:
            preview = int(value * 1000)

    video_offset = 0
    if headers.get("MOVIEOFFSET", "").strip():
        value = parse_real(headers["MOVIEOFFSET"])
        if value is None:
            diag.warning(f"MOVIEOFFSET '{headers['MOVIEOFFSET']}' is not a number and is ignored")
        else:
            video_offset = round(value * 1000)

    return osu.Metadata(
        title=title, title_unicode=title_unicode,
        artist=_clean(artist), artist_unicode=_clean(artist_unicode),
        creator=_clean(creator), version="", source=_clean(source), tags=tags,
        audio=headers.get("WAVE", "").strip().replace("\\", "/"),
        preview_time=preview,
        background=headers.get("BGIMAGE", "").strip().replace("\\", "/") or None,
        video=headers.get("BGMOVIE", "").strip().replace("\\", "/") or None,
        video_offset=video_offset, hp=0, od=0,
    )


def safe_filename(name: str) -> str:
    name = _BAD_FILENAME.sub("_", name).strip().rstrip(".")
    return name[:180] or "untitled"


# Conversion

def _version_name(chart: ChartSource, same_course_count: int, used: set[str]) -> str:
    version = VERSION_NAMES[chart.course]
    if chart.player:
        version += f" ({chart.player})"
    elif same_course_count > 1 and chart.headers.get("STYLE", "").strip().lower() in ("double", "couple", "2"):
        version += " (Double)"
    base, n = version, 2
    while version in used:
        version = f"{base} {n}"
        n += 1
    used.add(version)
    return version


@dataclass
class ParsedChart:
    version: str
    source: ChartSource
    events: list
    overlaps: bool
    dan_songs: list[DanSong]
    audio: str  # AudioFilename written to the .osu

    @property
    def is_dan(self) -> bool:
        return self.source.course == "dan"


def convert_file(path: Path, options: Options) -> FileResult:
    diag = Diagnostics()
    result = FileResult(path, diag)

    text = decode_text(path.read_bytes(), options.encoding, diag)
    charts = split_charts(text, diag)

    course_counts: dict[str, int] = {}
    for chart in charts:
        if chart.course:
            course_counts[chart.course] = course_counts.get(chart.course, 0) + 1

    # Parse (and validate) every selected chart before writing anything.
    parsed: list[ParsedChart] = []
    used_versions: set[str] = set()
    ordered = sorted(charts, key=lambda c: COURSE_ORDER.index(c.course) if c.course else len(COURSE_ORDER))
    for chart in ordered:
        if chart.course is None:
            diag.error(f"unknown COURSE '{chart.course_value}'", chart.start_line)
            continue
        if options.courses and chart.course not in options.courses:
            continue
        version = _version_name(chart, course_counts[chart.course], used_versions)
        scoped = diag.scoped(version)
        dan = chart.course == "dan"
        parser = NotationParser(chart.headers, options.branch, scoped, dan=dan)
        events = parser.run(chart.body)
        if not any(isinstance(e, (Hit, Roll, Swell)) for e in events):
            scoped.warning("the chart has no notes and is skipped", chart.start_line)
            continue
        audio = chart.headers.get("WAVE", "").strip().replace("\\", "/")
        if dan:
            if not parser.dan_songs:
                scoped.error("a dan course needs a #NEXTSONG for each song", chart.start_line)
            exams = sorted(k for k in chart.headers if k.startswith("EXAM"))
            if exams:
                scoped.warning(f"pass conditions ({', '.join(exams)}) cannot be represented in osu! and are ignored")
            audio = safe_filename(version.lower().replace(" ", "_")) + ".ogg"
        parsed.append(ParsedChart(version, chart, events, parser.overlaps, parser.dan_songs, audio))

    if not parsed and not diag.has_errors():
        diag.error("nothing to convert")

    # All difficulties share one set of files, so metadata comes from the first chart.
    meta = build_metadata(parsed[0].source.headers if parsed else (charts[0].headers if charts else {}), options, diag)
    assets, audio_jobs = _collect_assets(path, meta, parsed, diag)

    if options.strict:
        diag.promote_warnings()
    if diag.has_errors() and not options.force:
        return result

    with tempfile.TemporaryDirectory(prefix="tja2osz-") as temp:
        for rel, (parts, scope) in audio_jobs.items():
            written = Path(temp) / rel
            written.parent.mkdir(parents=True, exist_ok=True)
            if build_audio(parts, written, diag.scoped(scope)):
                assets[rel] = written
        if diag.has_errors() and not options.force:
            return result
        _convert_charts(parsed, meta, options, diag, result)
        if diag.has_errors() and not options.force:
            return result
        if not result.difficulties:
            return result
        result.outputs = _write(path, meta, result.difficulties, assets, options)
    result.converted = True
    return result


def _convert_charts(parsed: list[ParsedChart], meta: osu.Metadata, options: Options, diag: Diagnostics,
                    result: FileResult) -> None:
    for chart in parsed:
        version, events, overlaps = chart.version, chart.events, chart.overlaps
        scoped = diag.scoped(version)
        hp, od = DEFAULT_HP_OD[chart.source.course]
        chart_meta = osu.Metadata(**{**meta.__dict__, "version": version, "audio": chart.audio,
                                     "hp": options.hp if options.hp is not None else hp,
                                     "od": options.od if options.od is not None else od})
        osu_options = osu.OsuOptions(options.target, options.slider_multiplier, options.tick_rate)
        try:
            rendered = osu.render(events, chart_meta, osu_options)
        except ConversionError as e:
            scoped.error(str(e))
            continue
        text = rendered.text
        check = verify(text, events, options.target)
        for message in check.timing_problems:
            scoped.error(f"verification failed: {message}")
        # Appearance differences are expected only where osu! has no way to show the chart as is.
        known_limits = []
        if overlaps:
            known_limits.append("parts of the chart overlap in time (negative #DELAY or branch sections of "
                                "different lengths) but osu! has one scroll speed and bar line timeline")
        if rendered.merged_timing_points:
            known_limits.append("changes less than 1 ms apart had to share whole-millisecond timing points "
                                "(use --target lazer to keep them apart)")
        if check.appearance_problems and known_limits:
            for reason in known_limits:
                scoped.warning(f"{reason}; scroll speed, kiai or bar lines differ for some notes")
            for message in check.appearance_problems:
                scoped.info(f"differs: {message}")
        else:
            for message in check.appearance_problems:
                scoped.error(f"verification failed: {message}")
        filename = safe_filename(f"{meta.artist} - {meta.title} ({meta.creator}) [{version}]") + ".osu"
        result.difficulties.append(Difficulty(version, filename, text, check.notes,
                                              check.max_note_error, check.max_end_error))


def _collect_assets(path: Path, meta: osu.Metadata, parsed: list[ParsedChart],
                    diag: Diagnostics) -> tuple[dict[str, Path], dict[str, tuple[list[SongPart], str]]]:
    """Find the files to package and the audio that has to be written with ffmpeg.

    Returns (packaged path -> source file, packaged path -> (audio parts, diagnostics scope)).
    """
    assets: dict[str, Path] = {}
    audio_jobs: dict[str, tuple[list[SongPart], str]] = {}

    # Normal courses play WAVE, converted to Ogg Vorbis when osu! cannot play it.
    waves = {c.audio for c in parsed if not c.is_dan}
    if len(waves) > 1:
        diag.error("difficulties use different WAVE files")
    for wave in waves:
        source = path.parent / wave
        if not wave:
            diag.error("WAVE is missing")
            continue
        if not source.is_file():
            diag.error(f"WAVE file '{wave}' was not found")
            continue
        playable = playable_by_osu(source)
        if playable is None and Path(wave).suffix.lower() in (".mp3", ".ogg"):
            reason = "ffprobe could not read it" if find_ffmpeg() else "ffprobe is not available"
            diag.warning(f"the codec of '{wave}' was not checked ({reason}); it is included as is")
            playable = True
        if playable:
            assets[wave] = source
            continue
        if find_ffmpeg() is None:
            diag.error(f"osu! only plays MP3 and Ogg Vorbis; converting '{wave}' needs ffmpeg and ffprobe on PATH")
            continue
        converted = Path(wave).with_suffix(".ogg").as_posix()
        if converted == wave or (path.parent / converted).exists():
            converted = Path(wave).with_name(Path(wave).stem + "_osu.ogg").as_posix()
        diag.info(f"osu! only plays MP3 and Ogg Vorbis; '{wave}' is converted to '{converted}'")
        audio_jobs[converted] = ([SongPart(source, Fraction(0), None)], "")
        for chart in parsed:
            if not chart.is_dan and chart.audio == wave:
                chart.audio = converted

    # Dan courses get one file joined from their #NEXTSONG audio.
    for chart in parsed:
        if not chart.is_dan or not chart.dan_songs:
            continue
        missing = False
        for song in chart.dan_songs:
            if not (path.parent / song.wave).is_file():
                diag.scoped(chart.version).error(f"#NEXTSONG audio '{song.wave}' was not found", song.line)
                missing = True
        if not missing:
            parts = [SongPart(path.parent / s.wave, s.audio_start, s.line) for s in chart.dan_songs]
            audio_jobs[chart.audio] = (parts, chart.version)
    if any(c.is_dan for c in parsed) and find_ffmpeg() is None:
        diag.error("ffmpeg and ffprobe are needed to join the songs of a dan course; install them and add them to PATH")

    for rel, header in ((meta.background, "BGIMAGE"), (meta.video, "BGMOVIE")):
        if not rel:
            continue
        if (path.parent / rel).is_file():
            assets[rel] = path.parent / rel
        else:
            diag.warning(f"{header} file '{rel}' was not found and is not included")
    return assets, audio_jobs


def _write(path: Path, meta: osu.Metadata, difficulties: list[Difficulty], assets: dict[str, Path],
           options: Options) -> list[Path]:
    out_dir = options.output or path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    set_name = safe_filename(f"{meta.artist} - {meta.title}")
    outputs = []

    if options.write_folder:
        folder = out_dir / set_name
        folder.mkdir(parents=True, exist_ok=True)
        for d in difficulties:
            (folder / d.filename).write_bytes(d.text.encode("utf-8"))
        for rel, source in assets.items():
            target = folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.resolve() != source.resolve():
                target.write_bytes(source.read_bytes())
        outputs.append(folder)

    if options.write_osz:
        osz = out_dir / f"{set_name}.osz"
        with zipfile.ZipFile(osz, "w", zipfile.ZIP_DEFLATED) as archive:
            for d in difficulties:
                archive.writestr(d.filename, d.text.encode("utf-8"))
            for rel, source in assets.items():
                archive.write(source, rel)
        outputs.append(osz)
    return outputs
