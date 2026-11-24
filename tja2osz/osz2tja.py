"""Converts osu!taiko beatmaps (.osz / .osu) into TJA charts (Taiko-san Jiro, TJAPlayer3, OpenTaiko).

The .osu files are read the way osu!lazer reads them (lazer.py), so note times, drum roll lengths,
scroll speed, kiai and bar lines are what osu! shows. TJA places notes on an even grid per
measure, so every measure gets the smallest division that puts its notes within the snap
tolerance of their osu! times.

  red lines                -> BPM / #BPMCHANGE / #MEASURE (a red line in the middle of a measure
                              shortens that measure; any remainder becomes #DELAY)
  slider velocity          -> #SCROLL (scaled by SliderMultiplier / 1.4)
  kiai                     -> #GOGOSTART / #GOGOEND
  osu! bar lines           -> #BARLINEOFF / #BARLINEON
  circles (clap/whistle = ka, finish = big) -> 1 2 3 4
  sliders                  -> 5 / 6 ... 8
  spinners                 -> 7 ... 8, with BALLOON hit counts from osu!lazer's formula

The written .tja is read back with the TJA parser (tja.py) and compared with the beatmap.
"""
from __future__ import annotations

import argparse
import bisect
import math
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

from . import __version__, lazer
from .audio import SongPart, build_audio
from .convert import safe_filename
from .diagnostics import Diagnostics, ScopedDiagnostics
from .timeline import GogoChange, Hit, Measure, Roll, Swell, sort_events
from .tja import NotationParser, split_charts

MAX_DIVISION = 192
SYNC_EPSILON = Fraction(1, 2000)  # ms; smaller differences between TJA and osu! are not corrected
DEFAULT_SNAP_TOLERANCE = 2.0      # ms
BASE_SLIDER_MULTIPLIER = Fraction("1.4")  # osu!taiko's default; SliderMultiplier 1.4 = #SCROLL 1
SCROLL_PLACES = 4

COURSE_ORDER = ["edit", "oni", "hard", "normal", "easy"]  # order of the courses in the .tja
COURSE_NAMES = {"easy": "Easy", "normal": "Normal", "hard": "Hard", "oni": "Oni", "edit": "Edit"}
COURSE_ALIASES = {"easy": "easy", "kantan": "easy", "normal": "normal", "futsuu": "normal",
                  "hard": "hard", "muzukashii": "hard", "oni": "oni", "edit": "edit", "ura": "edit",
                  "inner": "edit"}
DEFAULT_LEVELS = {"easy": 3, "normal": 5, "hard": 7, "oni": 8, "edit": 9}
# Difficulty names -> course, checked in this order ("Inner Oni" is Edit, not Oni).
COURSE_PATTERNS = [
    ("edit", re.compile(r"inner|\bura\b|裏|\bedit\b", re.I)),
    ("oni", re.compile(r"\boni\b|鬼", re.I)),
    ("hard", re.compile(r"muzu|\bhard\b|難しい|むずかしい", re.I)),
    ("normal", re.compile(r"futsu|\bnormal\b|普通|ふつう", re.I)),
    ("easy", re.compile(r"kantan|\beasy\b|簡単|かんたん", re.I)),
]

# osu!lazer TaikoBeatmapConverter: swell RequiredHits
SWELL_HIT_MULTIPLIER = 1.65

# Beat snaps (1/s of a beat), simplest first: osu!'s editor snaps, then finer ones that charts
# converted from TJA use (1/24 and 1/48 of a beat appear in 96 / 192 divisions).
EDITOR_SNAPS = (1, 2, 3, 4, 6, 8, 12, 16, 5, 7, 9)
FINE_SNAPS = (24, 32, 48, 10, 14, 15, 18, 20, 21, 28)


# Numbers

def fmt_decimal(value, places: int) -> str:
    """Exact decimal text of `value` rounded to `places` digits, without trailing zeros."""
    q = round(Fraction(value), places)
    scaled = abs(q) * 10 ** places
    digits = str(int(scaled)).rjust(places + 1, "0")
    whole, frac = (digits[:-places], digits[-places:].rstrip("0")) if places else (digits, "")
    sign = "-" if q < 0 else ""
    return sign + whole + ("." + frac if frac else "")


def _bpm_text(beat_length: float, span_ms: float) -> str:
    """Shortest BPM text whose beat drifts less than 0.001 ms from osu!'s over the section."""
    beat = Fraction(beat_length)
    exact = Fraction(60000) / beat
    beats = Fraction(max(span_ms, 0.0)) / beat + 1
    for places in range(0, 13):
        text = fmt_decimal(exact, places)
        bpm = Fraction(text)
        if bpm > 0 and abs(Fraction(60000) / bpm - beat) * beats < Fraction(1, 1000):
            return text
    return fmt_decimal(exact, 12)


def _measure_text(num: int, den: int) -> str:
    ratio = Fraction(num, den)
    n, d = ratio.numerator, ratio.denominator
    while d < 4:
        n, d = n * 2, d * 2
    return f"{n}/{d}"


def difficulty_range(difficulty: float, low: float, mid: float, high: float) -> float:
    """IBeatmapDifficultyInfo.DifficultyRange"""
    if difficulty > 5:
        return mid + (high - mid) * (difficulty - 5) / 5
    if difficulty < 5:
        return mid + (mid - low) * (difficulty - 5) / 5
    return mid


def swell_hits(duration_ms: float, od: float) -> int:
    return int(max(1.0, duration_ms / 1000 * difficulty_range(od, 3, 5, 7.5) * SWELL_HIT_MULTIPLIER))


# Input

@dataclass
class OsuFile:
    name: str
    text: str


@dataclass
class BeatmapSet:
    label: str
    files: list[OsuFile]
    read: Callable[[str], "bytes | None"]  # file inside the set (audio) by its relative path


def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def _osz_set(path: Path) -> BeatmapSet:
    with zipfile.ZipFile(path) as archive:
        contents = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    by_key = {name.replace("\\", "/").lower(): data for name, data in contents.items()}
    files = [OsuFile(Path(name).name, _decode(data)) for name, data in contents.items() if name.lower().endswith(".osu")]
    return BeatmapSet(path.name, files, lambda rel: by_key.get(rel.replace("\\", "/").lower()))


def _folder_set(folder: Path, osu_files: list[Path]) -> BeatmapSet:
    def read(rel: str) -> bytes | None:
        target = folder / rel
        return target.read_bytes() if target.is_file() else None
    files = [OsuFile(p.name, _decode(p.read_bytes())) for p in osu_files]
    return BeatmapSet(str(folder), files, read)


def find_sets(inputs: list[str]) -> tuple[list[BeatmapSet], list[str]]:
    sets: list[BeatmapSet] = []
    missing: list[str] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() == ".osz":
            sets.append(_osz_set(path))
        elif path.is_file() and path.suffix.lower() == ".osu":
            sets.append(_folder_set(path.parent, [path]))
        elif path.is_dir():
            for osz in sorted(q for q in path.rglob("*") if q.is_file() and q.suffix.lower() == ".osz"):
                sets.append(_osz_set(osz))
            folders: dict[Path, list[Path]] = {}
            for osu_file in sorted(q for q in path.rglob("*") if q.is_file() and q.suffix.lower() == ".osu"):
                folders.setdefault(osu_file.parent, []).append(osu_file)
            for folder, files in folders.items():
                sets.append(_folder_set(folder, files))
        else:
            missing.append(raw)
    return sets, missing


# Options and results

@dataclass
class Options:
    output: Path = Path(".")
    courses: dict[str, str] = field(default_factory=dict)  # difficulty name (lower case) -> course
    levels: dict[str, int] = field(default_factory=dict)   # course -> LEVEL
    snap_tolerance: float = DEFAULT_SNAP_TOLERANCE
    offset_ms: float = 0.0          # added to every note time (positive = later)
    encoding: str = "cp932"
    use_slider_multiplier: bool = True
    audio: str = "copy"             # "copy" or "ogg"
    force: bool = False


@dataclass
class CourseResult:
    version: str
    course: str
    notes: int
    max_note_error: float
    max_end_error: float


@dataclass
class SetResult:
    label: str
    diagnostics: Diagnostics
    courses: list[CourseResult] = field(default_factory=list)
    output: Path | None = None
    converted: bool = False


# Beatmap

@dataclass
class Difficulty:
    file: OsuFile
    values: dict[str, str]
    points: lazer.ControlPoints
    objects: list[lazer.TaikoObject]
    version: str
    course: str = ""

    @property
    def od(self) -> float:
        return lazer.clamp(float(self.values.get("OverallDifficulty", "5")), 0, 10)

    @property
    def slider_multiplier(self) -> float:
        return lazer.clamp(float(self.values.get("SliderMultiplier", "1.4")),
                           lazer.MIN_SLIDER_MULTIPLIER, lazer.MAX_SLIDER_MULTIPLIER)


def read_difficulty(file: OsuFile, diag: Diagnostics) -> Difficulty | None:
    try:
        values, points, objects = lazer.parse_osu(file.text)
    except (ValueError, IndexError) as e:
        diag.error(f"'{file.name}' could not be read: {e}")
        return None
    version = values.get("Version", "") or Path(file.name).stem
    mode = values.get("Mode", "0").strip()
    if mode != "1":
        diag.info(f"not an osu!taiko difficulty (Mode {mode}); skipped", scope=version)
        return None
    if not points.timing:
        diag.error("no uninherited (red) timing point", scope=version)
        return None
    if not objects:
        diag.warning("no objects; skipped", scope=version)
        return None
    return Difficulty(file, values, points, objects, version)


def assign_courses(diffs: list[Difficulty], options: Options, diag: Diagnostics) -> list[Difficulty]:
    """Give every difficulty a TJA course: --course first, then its name, then its density."""
    used: dict[str, Difficulty] = {}
    unknown: list[Difficulty] = []
    for d in diffs:
        course = options.courses.get(d.version.lower())
        if course is None:
            course = next((c for c, pattern in COURSE_PATTERNS if pattern.search(d.version)), None)
        if course is None or course in used:
            if course in used:
                diag.warning(f"looks like {COURSE_NAMES[course]}, as [{used[course].version}] does", scope=d.version)
            unknown.append(d)
            continue
        d.course = course
        used[course] = d

    # The rest go to the free courses in order of note count (fewest notes -> easiest course),
    # the hardest ones first; Edit (ura) only when Easy to Oni are not enough.
    free = [c for c in reversed(COURSE_ORDER) if c not in used]
    main = [c for c in free if c != "edit"]
    unknown.sort(key=lambda d: len(d.objects))
    if len(unknown) > len(free):
        for d in unknown[:len(unknown) - len(free)]:
            diag.error(f"no free course left (a .tja holds Easy/Normal/Hard/Oni/Edit once each); "
                       f"choose with --course \"{d.version}=oni\" or convert it separately", scope=d.version)
        unknown = unknown[len(unknown) - len(free):]
    picked = main[len(main) - len(unknown):] if len(unknown) <= len(main) else free
    for d, course in zip(unknown, picked):
        d.course = course
        used[course] = d
        diag.warning(f"written as COURSE:{COURSE_NAMES[course]} (set it with --course \"{d.version}=...\")", scope=d.version)
    return sorted(used.values(), key=lambda d: COURSE_ORDER.index(d.course))


# Chart

@dataclass
class Bar:
    """One TJA measure."""
    osu_start: float
    beat_length: float
    bpm_text: str
    num: int
    den: int
    barline: bool = True
    tja_start: Fraction = Fraction(0)  # time the TJA puts it at (written numbers), ms from audio start
    length: Fraction = Fraction(0)
    delay_text: str = ""               # #DELAY written before the measure (seconds)
    division: int = 1

    def slot_time(self, k: int) -> float:
        return float(self.tja_start) + float(self.length) * k / self.division

    def exact_slot_time(self, slot: Fraction) -> Fraction:
        return self.tja_start + self.length * slot


@dataclass
class Item:
    time: float
    kind: str                          # "note", "end", "event"
    symbol: str = ""
    commands: list[str] = field(default_factory=list)
    obj: int = -1                      # index into the kept objects
    bar: int = 0
    pos: Fraction | None = None        # bar index + slot / division
    error: float = 0.0


@dataclass
class Expected:
    """A note as osu! plays it and where the written TJA should put it."""
    kind: str
    rim: bool
    big: bool
    osu_time: float
    osu_end: float
    tja_time: Fraction = Fraction(0)
    tja_end: Fraction = Fraction(0)
    scroll: Fraction = Fraction(1)
    kiai: bool = False
    beat_length: float = 0.0


@dataclass
class CourseChart:
    lines: list[str]
    balloons: list[int]
    expected: list[Expected]
    first_bpm_text: str
    bar_lines: list[float]   # osu! bar lines that the TJA must show (osu! time)
    range: tuple[float, float]


def chart_start(diff: Difficulty, tol: float) -> float:
    """Start of the first measure: the first red line, moved back whole measures for earlier notes."""
    red = diff.points.timing[0]
    bar_length = red.beat_length * red.meter
    start = red.time
    first = diff.objects[0].time
    if first < start - tol:
        start -= math.ceil((start - first - tol) / bar_length) * bar_length
    return start


class ChartBuilder:
    def __init__(self, diff: Difficulty, options: Options, diag: ScopedDiagnostics):
        self.diff = diff
        self.options = options
        self.diag = diag
        self.tol = options.snap_tolerance
        sm = Fraction(str(diff.slider_multiplier)) if options.use_slider_multiplier else BASE_SLIDER_MULTIPLIER
        self.scroll_factor = sm / BASE_SLIDER_MULTIPLIER

    def scroll_text(self, speed: float) -> str:
        return fmt_decimal(Fraction(speed) * self.scroll_factor, SCROLL_PLACES)

    # Objects

    def _objects(self) -> list[lazer.TaikoObject]:
        """Objects with drum rolls / spinners cut where the next object starts (TJA cannot overlap them)."""
        objects = [lazer.TaikoObject(o.kind, o.time, o.end, o.rim, o.big) for o in self.diff.objects]
        self.overlapping: dict[int, float] = {}  # object index -> end in osu!
        for i, o in enumerate(objects):
            if o.kind == "hit" or i + 1 >= len(objects):
                continue
            nxt = objects[i + 1].time
            if nxt < o.end + self.tol:  # also when it ends right on the next note
                self.overlapping[i] = o.end
                o.end = min(o.end, nxt)
        return objects

    # Measures

    def _partial_measure(self, remaining: float, beat_length: float) -> tuple[int, int, bool]:
        """Integer #MEASURE n/d for a measure cut short by a red line after `remaining` ms.

        Returns (n, d, fit): with fit, n/d is within half the snap tolerance of the cut and the
        measure's BPM is adjusted to end exactly there; otherwise n/d is a little short and the
        rest becomes #DELAY.
        """
        beats = Fraction(remaining) / Fraction(beat_length)
        half_tol = Fraction(self.tol) / 2
        for d in (4, 8, 16, 12, 24, 32, 48, 64, 96, MAX_DIVISION):
            n = math.floor(beats * d / 4 + Fraction(1, 2))
            if n >= 1 and abs((beats - Fraction(4 * n, d)) * Fraction(beat_length)) <= half_tol:
                return n, d, True
        return math.floor(beats * MAX_DIVISION / 4), MAX_DIVISION, False

    def _bars(self, start: float, last_time: float) -> list[Bar]:
        reds = self.diff.points.timing
        tol = self.tol
        bars: list[Bar] = []
        for i, red in enumerate(reds):
            sec_start = start if i == 0 else red.time
            sec_end = reds[i + 1].time if i + 1 < len(reds) else math.inf
            if sec_start > last_time + tol:
                break
            if sec_end <= sec_start + tol:
                continue
            bar_length = red.beat_length * red.meter
            bpm_text = _bpm_text(red.beat_length, min(sec_end, last_time + bar_length) - sec_start)
            k = 0
            while True:
                m_start = sec_start + k * bar_length
                if m_start >= sec_end - tol or m_start > last_time + tol:
                    break
                remaining = sec_end - m_start
                text = bpm_text
                if remaining >= bar_length - tol / 2:
                    num, den = red.meter, 4
                    if remaining < bar_length - 1e-6:
                        # The next red line comes a little before this bar line (whole-ms red
                        # lines do). TJA cannot go back in time, so this one measure is played a
                        # hair faster to end exactly there.
                        text = _bpm_text(remaining / red.meter, remaining)
                else:
                    num, den, fit = self._partial_measure(remaining, red.beat_length)
                    if num == 0:
                        break
                    beats = 4 * num / den
                    if fit and abs(remaining - red.beat_length * beats) > 1e-6:
                        text = _bpm_text(remaining / beats, remaining)
                bars.append(Bar(m_start, red.beat_length, text, num, den))
                k += 1
        # One empty measure after the last object, so that roll ends on a bar line have a place.
        last = bars[-1]
        bars.append(Bar(last.osu_start + last.beat_length * 4 * last.num / last.den, last.beat_length,
                        last.bpm_text, last.num, last.den))
        return bars

    def _time_bars(self, bars: list[Bar], tja_time: Fraction) -> None:
        """Place the bars on the TJA timeline, adding #DELAY where the TJA would fall behind osu!."""
        worst_lead = Fraction(0)
        for bar in bars:
            delta = Fraction(bar.osu_start) - tja_time
            if delta > SYNC_EPSILON:
                bar.delay_text = fmt_decimal(delta / 1000, 9)
                tja_time += Fraction(bar.delay_text) * 1000
            elif delta < 0:
                worst_lead = max(worst_lead, -delta)
            bar.tja_start = tja_time
            bar.length = Fraction(240000) / Fraction(bar.bpm_text) * Fraction(bar.num, bar.den)
            tja_time += bar.length
        if worst_lead > Fraction(self.tol):
            self.diag.warning(f"red lines just before a bar line make some measures up to {float(worst_lead):.3f} ms late")

    # Placement

    def _snap(self, bar: Bar, t: float, at_end: bool) -> Fraction | None:
        """Position of `t` in the measure (0 to 1) on the simplest beat snap within the tolerance.

        Snaps are tried from the simplest (1/1, 1/2, 1/3, 1/4, ...), so that a note is never read
        as an odd division (say 27/37 of a measure) that happens to be within the tolerance too.
        Measures always start on a beat of their red line, as osu!'s snaps do.
        """
        beats = Fraction(4 * bar.num, bar.den)
        beat = float(bar.length) / float(beats)
        start = float(bar.tja_start)
        x = (t - start) / beat
        tol = self.tol + (1.0 if at_end else 0.0)  # roll ends add osu!'s whole-ms roll length
        options: list[tuple[int, Fraction, float]] = []
        for s in EDITOR_SNAPS + FINE_SNAPS:
            pos = Fraction(min(max(math.floor(x * s + 0.5), 0), math.floor(beats * s)), s)
            if pos >= beats and not at_end:
                pos -= Fraction(1, s)
            options.append((s, pos, abs(start + beat * float(pos) - t)))
        # 1. exactly on a snap (fractional-ms beatmaps), simplest snap first
        for s, pos, err in options:
            if err <= 1e-3:
                return pos / beats
        # 2. the closest editor snap, then the closest fine snap. A snap only counts when the note
        #    is much closer to it than its spacing, so that a note a few ms off a beat is not
        #    taken for a 1/48 note.
        for group in (EDITOR_SNAPS, FINE_SNAPS):
            fits = [(round(err, 6), i, pos) for i, (s, pos, err) in enumerate(options)
                    if s in group and err <= min(tol, beat / s / 8)]
            if fits:
                return min(fits)[2] / beats
        return None

    @staticmethod
    def _preferred(bars: list[Bar], item: Item, snap: Fraction | None,
                   ok: Callable[[Fraction], bool]) -> tuple[Fraction, float] | None:
        """The item's snapped position, if it has one that satisfies `ok`."""
        if snap is None:
            return None
        pos = item.bar + snap
        if not ok(pos):
            return None
        b = math.floor(pos)
        return pos, abs(float(bars[b].exact_slot_time(pos - b)) - item.time)

    def _place(self, bars: list[Bar], t: float, bar_index: int, ok: Callable[[Fraction], bool],
               refine: int) -> tuple[Fraction, float] | None:
        """Grid position nearest to `t` that satisfies `ok`, refining bar `refine` when there is none."""
        for _ in range(4):
            best: tuple[float, Fraction] | None = None
            for b in (bar_index - 1, bar_index, bar_index + 1):
                if not 0 <= b < len(bars):
                    continue
                bar = bars[b]
                for k in range(bar.division):
                    pos = b + Fraction(k, bar.division)
                    if ok(pos):
                        err = abs(bar.slot_time(k) - t)
                        if best is None or err < best[0]:
                            best = (err, pos)
            if best is not None:
                return best[1], best[0]
            if not 0 <= refine < len(bars):
                break
            bars[refine].division *= 2
        return None

    # Build

    def build(self, tja_start: Fraction) -> CourseChart:
        diff, tol = self.diff, self.tol
        objects = self._objects()
        last_time = max(max(o.time, o.end) for o in objects)
        start = chart_start(diff, tol)
        bars = self._bars(start, last_time)
        self._time_bars(bars, tja_start)
        starts = [b.osu_start for b in bars]

        def bar_of(t: float) -> int:
            return max(0, min(bisect.bisect_right(starts, t + tol) - 1, len(bars) - 1))

        # osu! bar lines. With whole-ms red lines osu! can draw the line a fraction of a ms before
        # the red line (from the previous one's grid), so lines within the tolerance count.
        lines = lazer.generate_bar_lines(diff.points.timing, objects[0].time, last_time)
        for bar in bars:
            i = bisect.bisect_left(lines, bar.osu_start - tol)
            bar.barline = i < len(lines) and lines[i] <= bar.osu_start + tol

        # Notes, roll ends, scroll / kiai changes
        notes: list[Item] = []
        ends: list[Item] = []
        for i, o in enumerate(objects):
            if o.kind == "hit":
                symbol = ("4" if o.big else "2") if o.rim else ("3" if o.big else "1")
            elif o.kind == "roll":
                symbol = "6" if o.big else "5"
            else:
                symbol = "7"
            notes.append(Item(o.time, "note", symbol, obj=i, bar=bar_of(o.time)))
            if o.kind != "hit":
                ends.append(Item(o.end, "end", "8", obj=i, bar=bar_of(o.end)))

        events: list[Item] = []
        scroll, kiai = "1", False
        for effect in diff.points.effect:
            if effect.time > last_time:
                break
            commands = []
            text = self.scroll_text(effect.scroll_speed)
            if text != scroll:
                commands.append(f"#SCROLL {text}")
                scroll = text
            if effect.kiai != kiai:
                commands.append("#GOGOSTART" if effect.kiai else "#GOGOEND")
                kiai = effect.kiai
            if commands:
                events.append(Item(effect.time, "event", commands=commands, bar=bar_of(effect.time)))

        # Snap everything to beat snaps; each measure gets the division its snaps need.
        snaps: dict[int, Fraction | None] = {}
        for item in notes + ends + events:
            snap = self._snap(bars[item.bar], item.time, at_end=item.kind != "note")
            snaps[id(item)] = snap
            if snap is not None:
                bars[item.bar].division = math.lcm(bars[item.bar].division, snap.denominator)

        # Notes. Two notes on one position cannot be written; the later one is dropped.
        kept: list[Item] = []
        taken: set[Fraction] = set()
        dropped: set[int] = set()
        off_snap: list[Item] = []
        for item in notes:
            bar = bars[item.bar]
            snap = snaps[id(item)]
            pos = None
            if snap is not None:
                pos = item.bar + snap
            else:
                # Not on any snap: nearest free position of the measure's grid (at least 1/4 beat),
                # made finer only when that position is taken.
                quarter_beat = (Fraction(1, 4) / Fraction(4 * bar.num, bar.den)).denominator
                bar.division = math.lcm(bar.division, quarter_beat)
                for _ in range(4):
                    k = math.floor((item.time - float(bar.tja_start)) / float(bar.length) * bar.division + 0.5)
                    candidate = item.bar + Fraction(min(max(k, 0), bar.division - 1), bar.division)
                    if candidate not in taken:
                        pos = candidate
                        break
                    bar.division *= 2
                off_snap.append(item)
            if pos is None or pos in taken:
                self.diag.warning(f"the object at {item.time:g} ms falls on the same TJA position as the one before "
                                  "it and is dropped")
                dropped.add(item.obj)
                if item in off_snap:
                    off_snap.remove(item)
                continue
            item.pos = pos
            item.error = abs(float(bar.exact_slot_time(pos - item.bar)) - item.time)
            taken.add(pos)
            kept.append(item)
        if off_snap:
            worst = max(off_snap, key=lambda i: i.error)
            self.diag.warning(f"{len(off_snap)} note(s) are not on a beat snap and are moved to the nearest grid "
                              f"position (up to {worst.error:.3f} ms, at {worst.time:g} ms; first at {off_snap[0].time:g} ms)")
        pos_of = {item.obj: item.pos for item in kept}
        kept_times = [item.time for item in kept]
        kept_pos = [item.pos for item in kept]

        # Roll ends: after the roll's head and before the next note.
        placed_ends: list[Item] = []
        for item in ends:
            if item.obj in dropped:
                continue
            head = pos_of[item.obj]
            index = kept_pos.index(head)
            nxt = kept_pos[index + 1] if index + 1 < len(kept_pos) else None

            def ok(p: Fraction, head=head, nxt=nxt) -> bool:
                return head < p and (nxt is None or p < nxt)
            placed = self._preferred(bars, item, snaps[id(item)], ok)
            if placed is None:
                placed = self._place(bars, item.time, item.bar, ok,
                                     refine=math.floor(head) if item.bar - 1 <= math.floor(head) else item.bar)
            if placed is None:
                self.diag.error(f"no place for the end of the drum roll / spinner at {objects[item.obj].time:g} ms")
                continue
            item.pos, item.error = placed
            o = objects[item.obj]
            what = "drum roll" if o.kind == "roll" else "spinner"
            b = math.floor(item.pos)
            written_end = float(bars[b].exact_slot_time(item.pos - b))
            if item.obj in self.overlapping:
                self.diag.warning(f"the {what} at {o.time:g} ms runs into the next object; it ends "
                                  f"{self.overlapping[item.obj] - written_end:.3f} ms earlier in the TJA")
            elif item.error > tol:
                self.diag.warning(f"the end of the {what} at {o.time:g} ms is moved {item.error:.3f} ms to the grid")
            placed_ends.append(item)

        # Scroll / kiai changes: keep them between the same notes as in osu!.
        previous: Fraction | None = None
        for item in events:
            i = bisect.bisect_left(kept_times, item.time)
            before = kept_pos[i - 1] if i > 0 else None
            after = kept_pos[i] if i < len(kept_pos) else None
            low = previous

            def ok(p: Fraction, before=before, after=after, low=low) -> bool:
                return ((before is None or p > before) and (after is None or p <= after)
                        and (low is None or p >= low))
            placed = self._preferred(bars, item, snaps[id(item)], ok)
            if placed is None:
                placed = self._place(bars, item.time, item.bar, ok,
                                     refine=math.floor(before) if before is not None else item.bar)
            if placed is None:
                self.diag.error(f"no place for the scroll / kiai change at {item.time:g} ms")
                continue
            item.pos, item.error = placed
            previous = item.pos

        lines_out = self._write(bars, kept, placed_ends, [e for e in events if e.pos is not None])

        # What the TJA should contain, for verification
        expected: list[Expected] = []
        end_of = {item.obj: item for item in placed_ends}
        for item in kept:
            o = objects[item.obj]
            exp = Expected(o.kind, o.rim, o.big, o.time, o.end)
            b = math.floor(item.pos)
            exp.tja_time = bars[b].exact_slot_time(item.pos - b)
            if item.obj in end_of:
                e = end_of[item.obj].pos
                exp.tja_end = bars[math.floor(e)].exact_slot_time(e - math.floor(e))
            effect = diff.points.effect_at(o.time)
            exp.scroll = Fraction(self.scroll_text(effect.scroll_speed))
            exp.kiai = effect.kiai
            exp.beat_length = diff.points.timing_at(o.time).beat_length
            expected.append(exp)

        balloons = [swell_hits(objects[i.obj].end - objects[i.obj].time, diff.od)
                    for i in kept if objects[i.obj].kind == "swell"]
        return CourseChart(lines_out, balloons, expected, bars[0].bpm_text, lines,
                           (objects[0].time, last_time))

    def _write(self, bars: list[Bar], notes: list[Item], ends: list[Item], events: list[Item]) -> list[str]:
        by_bar: dict[int, list[Item]] = {}
        for item in notes + ends + events:
            by_bar.setdefault(math.floor(item.pos), []).append(item)

        out: list[str] = []
        bpm, measure, barline = None, Fraction(1), True
        for b, bar in enumerate(bars):
            items = by_bar.get(b, [])
            n = bar.division
            slots = [int((item.pos - b) * n) for item in items]
            g = n
            for k in slots:
                g = math.gcd(g, k)
            n //= g
            chars = ["0"] * n
            commands: dict[int, list[str]] = {}
            for item, k in zip(items, slots):
                k //= g
                if item.kind == "event":
                    commands.setdefault(k, []).extend(item.commands)
                else:
                    chars[k] = item.symbol

            if bar.delay_text:
                out.append(f"#DELAY {bar.delay_text}")
            if bar.bpm_text != bpm:
                if bpm is not None:
                    out.append(f"#BPMCHANGE {bar.bpm_text}")
                bpm = bar.bpm_text
            if Fraction(bar.num, bar.den) != measure:
                measure = Fraction(bar.num, bar.den)
                out.append(f"#MEASURE {_measure_text(bar.num, bar.den)}")
            if bar.barline != barline:
                barline = bar.barline
                out.append("#BARLINEON" if barline else "#BARLINEOFF")
            out.extend(commands.pop(0, []))
            previous = 0
            for k in sorted(commands):
                out.append("".join(chars[previous:k]))
                out.extend(commands[k])
                previous = k
            out.append("".join(chars[previous:]) + ",")
        return out


# Verification

@dataclass
class Check:
    max_note_error: float = 0.0
    max_end_error: float = 0.0


def verify_course(events: list, chart: CourseChart, offset_ms: float, tolerance: float,
                  diag: ScopedDiagnostics) -> Check:
    """Compare what the TJA parser reads from the written chart with the beatmap."""
    check = Check()
    shift = Fraction(offset_ms)
    events = sort_events(events)
    got = [e for e in events if isinstance(e, (Hit, Roll, Swell))]
    gogo = [e for e in events if isinstance(e, GogoChange)]
    gogo_times = [e.time for e in gogo]
    exact_problems, look_problems = [], []

    if len(got) != len(chart.expected):
        diag.error(f"verification failed: {len(got)} notes in the TJA, {len(chart.expected)} expected")
        return check
    for exp, e in zip(chart.expected, got):
        kind = "hit" if isinstance(e, Hit) else "roll" if isinstance(e, Roll) else "swell"
        rim = isinstance(e, Hit) and e.rim
        big = not isinstance(e, Swell) and e.big
        where = f"{exp.osu_time:g} ms"
        if (kind, rim, big) != (exp.kind, exp.rim, exp.big):
            exact_problems.append(f"{where}: {kind} (rim={rim}, big={big}) instead of {exp.kind}")
        if abs(e.time - shift - exp.tja_time) > Fraction(1, 10 ** 6):
            exact_problems.append(f"{where}: TJA time {float(e.time):.6f} instead of {float(exp.tja_time + shift):.6f}")
        check.max_note_error = max(check.max_note_error, abs(float(e.time - shift) - exp.osu_time))
        if kind != "hit":
            if abs(e.end - shift - exp.tja_end) > Fraction(1, 10 ** 6):
                exact_problems.append(f"{where}: TJA end {float(e.end):.6f} instead of {float(exp.tja_end + shift):.6f}")
            check.max_end_error = max(check.max_end_error, abs(float(e.end - shift) - exp.osu_end))
        if e.scroll != exp.scroll:
            look_problems.append(f"{where}: #SCROLL {float(e.scroll):g} instead of {float(exp.scroll):g}")
        # 1%: a measure that ends just before a red line is played slightly faster (see _bars).
        if abs(60000 / float(e.bpm) - exp.beat_length) > 1e-2 * exp.beat_length:
            look_problems.append(f"{where}: BPM {float(e.bpm):g} instead of {60000 / exp.beat_length:g}")
        i = bisect.bisect_right(gogo_times, e.time) - 1
        on = gogo[i].on if i >= 0 else False
        if on != exp.kiai:
            look_problems.append(f"{where}: go-go time is {'on' if on else 'off'} but kiai is {'on' if exp.kiai else 'off'}")

    # Bar lines from the first to the last object must match (within the tolerance).
    low, high = chart.range
    tja_lines = sorted(float(e.time - shift) for e in events if isinstance(e, Measure) and e.barline)

    def near(values: list[float], t: float) -> bool:
        i = bisect.bisect_left(values, t - tolerance)
        return i < len(values) and values[i] <= t + tolerance

    for t in chart.bar_lines:
        if low - tolerance <= t <= high and not near(tja_lines, t):
            look_problems.append(f"missing bar line at {t:.3f} ms")
    for t in tja_lines:
        if low <= t <= high and not near(chart.bar_lines, t):
            look_problems.append(f"extra bar line at {t:.3f} ms")

    for message in exact_problems[:20]:
        diag.error(f"verification failed: {message}")
    for message in look_problems[:20]:
        diag.warning(f"differs from osu!: {message}")
    return check


# Set conversion

def _encodable(text: str, encoding: str) -> bool:
    try:
        text.encode(encoding)
        return True
    except UnicodeEncodeError:
        return False


def _header_value(name: str, value: str, encoding: str, diag: Diagnostics) -> str:
    value = value.replace("\r", " ").replace("\n", " ").strip()
    if _encodable(value, encoding):
        return value
    bad = "".join(sorted({c for c in value if not _encodable(c, encoding)}))
    diag.warning(f"{name} has characters {encoding} cannot hold ({bad}); they become '?' (try --encoding utf-8)")
    return value.encode(encoding, errors="replace").decode(encoding)


def convert_set(beatmap_set: BeatmapSet, options: Options) -> SetResult:
    diag = Diagnostics()
    result = SetResult(beatmap_set.label, diag)
    diffs = [d for d in (read_difficulty(f, diag) for f in beatmap_set.files) if d is not None]
    if not diffs:
        if not diag.has_errors():
            diag.error("no osu!taiko difficulty found")
        return result
    diffs = assign_courses(diffs, options, diag)
    if not diffs:
        return result

    first = diffs[0].values
    title_unicode = first.get("TitleUnicode", "") or first.get("Title", "")
    title = first.get("Title", "")
    artist_unicode = first.get("ArtistUnicode", "") or first.get("Artist", "")
    artist = first.get("Artist", "")
    audio = first.get("AudioFilename", "").strip()
    if len({d.values.get("AudioFilename", "").strip() for d in diffs}) > 1:
        diag.warning("difficulties use different audio files; the first one is used for every course")
    enc = options.encoding

    # Audio
    audio_data = beatmap_set.read(audio) if audio else None
    if audio_data is None:
        diag.error(f"audio file '{audio}' was not found in the beatmap")
    wave = audio
    if options.audio == "ogg" and Path(audio).suffix.lower() != ".ogg":
        wave = Path(audio).stem + ".ogg"
    if not _encodable(wave, enc) or "/" in wave or "\\" in wave:
        wave = "song" + Path(wave).suffix.lower()
        diag.info(f"the audio file is renamed to '{wave}'")

    # One chart start for every course: the earliest one. Later courses wait with #DELAY.
    starts = {d.course: chart_start(d, options.snap_tolerance) for d in diffs}
    origin = min(starts.values())
    offset_text = fmt_decimal(-(Fraction(origin) + Fraction(options.offset_ms)) / 1000, 6)
    tja_origin = -Fraction(offset_text) * 1000 - Fraction(options.offset_ms)

    charts: dict[str, CourseChart] = {}
    for d in diffs:
        scoped = diag.scoped(d.version)
        charts[d.course] = ChartBuilder(d, options, scoped).build(tja_origin)

    header_bpm = charts[diffs[0].course].first_bpm_text
    preview = first.get("PreviewTime", "-1").strip()
    lines = [
        f"TITLE:{_header_value('TITLE', title_unicode, enc, diag)}",
    ]
    if title and title != title_unicode:
        lines.append(f"TITLEEN:{_header_value('TITLEEN', title, enc, diag)}")
    if artist_unicode:
        lines.append(f"SUBTITLE:--{_header_value('SUBTITLE', artist_unicode, enc, diag)}")
    if artist and artist != artist_unicode:
        lines.append(f"SUBTITLEEN:--{_header_value('SUBTITLEEN', artist, enc, diag)}")
    lines += [f"BPM:{header_bpm}", f"WAVE:{wave}", f"OFFSET:{offset_text}"]
    try:
        if float(preview) >= 0:
            lines.append(f"DEMOSTART:{fmt_decimal(Fraction(preview) / 1000, 3)}")
    except ValueError:
        pass
    creator = first.get("Creator", "")
    if creator:
        lines.append(f"MAKER:{_header_value('MAKER', creator, enc, diag)}")
    lines.append("")

    for d in diffs:
        chart = charts[d.course]
        level = options.levels.get(d.course, DEFAULT_LEVELS[d.course])
        if d.course not in options.levels:
            diag.info(f"LEVEL:{level} is a placeholder (set it with --level {d.course}=N)", scope=d.version)
        lines += [f"COURSE:{COURSE_NAMES[d.course]}", f"LEVEL:{level}",
                  f"BALLOON:{','.join(str(n) for n in chart.balloons)}", "", "#START"]
        if chart.first_bpm_text != header_bpm:
            lines.append(f"#BPMCHANGE {chart.first_bpm_text}")
        lines += chart.lines
        lines += ["#END", ""]
    text = "\r\n".join(lines)

    # Read the chart back and compare it with the beatmap.
    check_diag = Diagnostics()
    sources = split_charts(text, check_diag)
    for d, source in zip(diffs, sources):
        scoped = diag.scoped(d.version)
        parse_diag = Diagnostics()
        events = NotationParser(source.headers, "M", parse_diag.scoped("")).run(source.body)
        for item in parse_diag.items:
            if item.level != "info":
                scoped.error(f"the written TJA does not read back cleanly: line {item.line}: {item.message}")
        check = verify_course(events, charts[d.course], options.offset_ms, options.snap_tolerance, scoped)
        result.courses.append(CourseResult(d.version, COURSE_NAMES[d.course], len(charts[d.course].expected),
                                           check.max_note_error, check.max_end_error))
    if len(sources) != len(diffs):
        diag.error(f"verification failed: the written TJA has {len(sources)} courses, {len(diffs)} expected")

    if diag.has_errors() and not options.force:
        return result

    folder_name = safe_filename(title_unicode or title or Path(beatmap_set.label).stem)
    folder = options.output / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    tja_path = folder / f"{folder_name}.tja"
    data = text.encode("utf-8-sig" if enc.replace("_", "-").lower() in ("utf-8", "utf8") else enc, errors="replace")
    tja_path.write_bytes(data)
    if audio_data is not None:
        target = folder / wave
        if wave.lower().endswith(".ogg") and not audio.lower().endswith(".ogg"):
            with tempfile.TemporaryDirectory(prefix="osz2tja-") as temp:
                source = Path(temp) / ("source" + Path(audio).suffix)
                source.write_bytes(audio_data)
                build_audio([SongPart(source, Fraction(0), None)], target, diag.scoped(""))
        else:
            target.write_bytes(audio_data)
    result.output = tja_path
    result.converted = not diag.has_errors() or options.force
    return result


# Command line

def _pairs(values: list[str] | None, what: str, parser: argparse.ArgumentParser) -> list[tuple[str, str]]:
    pairs = []
    for raw in values or []:
        if "=" not in raw:
            parser.error(f"{what} needs NAME=VALUE (got '{raw}')")
        name, value = raw.rsplit("=", 1)
        pairs.append((name.strip(), value.strip()))
    return pairs


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="osz2tja",
        description="Convert osu!taiko beatmaps (.osz, .osu or beatmap folders) to TJA charts.",
        epilog="Exit status: 0 = all converted, 1 = bad arguments / no input, 2 = some sets failed.")
    p.add_argument("inputs", nargs="+", help=".osz / .osu files or folders (searched recursively)")
    p.add_argument("-o", "--output", type=Path, default=Path("."),
                   help="folder to write the song folders into (default: current folder)")
    p.add_argument("--course", action="append", metavar="DIFFICULTY=COURSE",
                   help="course for a difficulty, e.g. \"Insane=oni\" (easy/normal/hard/oni/edit); repeatable")
    p.add_argument("--level", action="append", metavar="COURSE=N", help="LEVEL of a course, e.g. oni=9; repeatable")
    p.add_argument("--snap-tolerance", type=float, default=DEFAULT_SNAP_TOLERANCE, metavar="MS",
                   help=f"how far a note may be from the TJA grid (default: {DEFAULT_SNAP_TOLERANCE:g} ms)")
    p.add_argument("--offset", type=float, default=0.0, metavar="MS",
                   help="move every note by this many ms (positive = later)")
    p.add_argument("--encoding", default="cp932",
                   help="TJA encoding: cp932 (Shift-JIS, for Taiko-san Jiro; default) or utf-8 (written with a BOM)")
    p.add_argument("--ignore-slider-multiplier", action="store_true",
                   help="write the slider velocity as #SCROLL as is, without scaling by SliderMultiplier / 1.4")
    p.add_argument("--audio", choices=["copy", "ogg"], default="copy",
                   help="copy the audio as is (default) or convert it to Ogg Vorbis with ffmpeg")
    p.add_argument("--force", action="store_true", help="write output even when there are errors")
    p.add_argument("-v", "--verbose", action="store_true", help="also show informational messages")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)

    courses = {}
    for name, value in _pairs(args.course, "--course", parser):
        course = COURSE_ALIASES.get(value.lower())
        if course is None:
            parser.error(f"unknown course '{value}' (easy/normal/hard/oni/edit)")
        courses[name.lower()] = course
    levels = {}
    for name, value in _pairs(args.level, "--level", parser):
        course = COURSE_ALIASES.get(name.lower())
        if course is None or not value.isdigit():
            parser.error(f"--level needs COURSE=NUMBER (got '{name}={value}')")
        levels[course] = int(value)
    try:
        "".encode(args.encoding)
    except LookupError:
        parser.error(f"unknown encoding '{args.encoding}'")
    if args.snap_tolerance <= 0:
        parser.error("--snap-tolerance must be positive")

    options = Options(output=args.output, courses=courses, levels=levels, snap_tolerance=args.snap_tolerance,
                      offset_ms=args.offset, encoding=args.encoding,
                      use_slider_multiplier=not args.ignore_slider_multiplier, audio=args.audio, force=args.force)

    sets, missing = find_sets(args.inputs)
    for raw in missing:
        print(f"error: not found: {raw}", file=sys.stderr)
    if not sets:
        print("error: no .osz / .osu files to convert", file=sys.stderr)
        return 1

    converted = 0
    for beatmap_set in sets:
        print(f"* {beatmap_set.label}")
        try:
            result = convert_set(beatmap_set, options)
        except (OSError, zipfile.BadZipFile) as e:
            print(f"  error   {e}")
            continue
        for line in result.diagnostics.format(args.verbose):
            print(line)
        for c in result.courses:
            end = f", roll/balloon ends <= {c.max_end_error:.3f} ms" if c.max_end_error else ""
            print(f"  -> [{c.version}] COURSE:{c.course}, {c.notes} notes, max time error {c.max_note_error:.3f} ms{end}")
        if result.converted:
            converted += 1
            print(f"  wrote {result.output}")
        else:
            print("  skipped: fix the errors above (or use --force)")
    print(f"Done: {converted}/{len(sets)} converted.")
    return 0 if converted == len(sets) and not missing else 2


if __name__ == "__main__":
    sys.exit(main())
