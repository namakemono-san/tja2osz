"""osu!taiko (.osu) writer.

Targets:
  lazer  - "osu file format v128" with fractional millisecond times. Everything lands on the
           exact TJA time except drum roll ends, which lazer truncates to whole-millisecond
           durations (error <= 0.5 ms).
  stable - "osu file format v14" with whole millisecond times, because osu!stable cannot read
           fractional times (see LegacyBeatmapExporter in osu!lazer). Error <= 0.5 ms.

How TJA concepts map to osu!taiko:
  BPM / #MEASURE / #BPMCHANGE -> uninherited (red) timing points
  #SCROLL                     -> inherited (green) timing points, re-added after every red one
                                 because a red point resets the scroll speed to 1
  #GOGOSTART / #GOGOEND       -> kiai flag
  bar lines / #BARLINEOFF     -> red points and meters chosen so that lazer's BarLineGenerator
                                 produces exactly the TJA bar lines (see _fix_bar_lines)
  1 2 3 4 A B                 -> hit circles (clap = ka, finish = big)
  5 6 H I ... 8               -> sliders whose length is solved for the exact duration
  7 9 D ... 8                 -> spinners
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from . import lazer
from .diagnostics import ConversionError
from .timeline import BpmChange, GogoChange, Hit, Measure, Roll, ScrollChange, Swell, sort_events

LAZER = "lazer"
STABLE = "stable"

HALF = Fraction(1, 2)


class TimeFormat:
    """Maps exact TJA times to the numbers written for a target."""

    def __init__(self, target: str) -> None:
        self.target = target
        # How far a generated bar line may be from the TJA one and still count as the same line.
        self.bar_tolerance = 1e-4 if target == LAZER else 1.0 + 1e-6

    def value(self, t: Fraction) -> float:
        if self.target == STABLE:
            return float(math.floor(t + HALF))  # round half up
        return float(t)

    def text(self, t: Fraction) -> str:
        return lazer.format_number(self.value(t))


def beat_length(bpm: Fraction) -> float:
    return lazer.clamp(float(Fraction(60000) / bpm), lazer.MIN_BEAT_LENGTH, lazer.MAX_BEAT_LENGTH)


def scroll_speed(value: Fraction) -> float:
    """TJA #SCROLL value as an osu! scroll speed (negative values lose their direction)."""
    speed = abs(float(value))
    return lazer.clamp(speed, lazer.MIN_SCROLL_SPEED, lazer.MAX_SCROLL_SPEED)


@dataclass
class RedPoint:
    time: Fraction
    bpm: Fraction
    meter: int
    omit: bool
    kiai: bool = False


@dataclass
class GreenPoint:
    time: Fraction
    scroll: float
    kiai: bool


@dataclass
class OsuOptions:
    target: str
    slider_multiplier: float
    tick_rate: float


# Timing points

def _meter_for(beats: Fraction) -> int:
    return max(1, math.ceil(beats))


def _plan_red_points(events: list) -> dict[Fraction, RedPoint]:
    """First pass: a red point wherever the BPM or measure grid changes, or a bar line is hidden."""
    reds: dict[Fraction, RedPoint] = {}
    current: RedPoint | None = None
    current_is_grid = False  # whether `current` marks whole measures of `current.meter` beats

    for ev in events:
        if isinstance(ev, Measure):
            whole = ev.beats.denominator == 1
            on_grid = False
            if current is not None and current_is_grid and current.bpm == ev.bpm and current.meter == ev.beats:
                bars = (ev.time - current.time) / (Fraction(60000) / ev.bpm * current.meter)
                on_grid = bars.denominator == 1
            if not (on_grid and whole and ev.barline):
                red = reds.get(ev.time)
                if red is None:
                    red = reds[ev.time] = RedPoint(ev.time, ev.bpm, _meter_for(ev.beats), not ev.barline)
                else:
                    red.bpm, red.meter, red.omit = ev.bpm, _meter_for(ev.beats), not ev.barline
                current, current_is_grid = red, whole and ev.barline
        elif isinstance(ev, BpmChange):
            red = reds.get(ev.time)
            if red is None:
                red = reds[ev.time] = RedPoint(ev.time, ev.bpm, 1, True)
            else:
                red.bpm = ev.bpm
            current, current_is_grid = red, False
    return reds


def _fix_bar_lines(reds: dict[Fraction, RedPoint], measures: list[Measure], fmt: TimeFormat,
                   first_hit: float, last_end: float) -> list[RedPoint]:
    """Adjust red points until lazer's BarLineGenerator yields exactly the TJA bar lines.

    BarLineGenerator draws a line every beatLength * meter from a red point up to *and including*
    the next red point, so a hidden bar line or a mid-measure BPM change needs its preceding
    grid to stop short. Problems are fixed one at a time, earliest first:
      missing line -> add a red point at that measure
      extra line   -> give the red point before it a meter long enough to skip it (splitting
                      the grid at the last wanted line if needed)
    """
    tol = fmt.bar_tolerance
    wanted = [(fmt.value(m.time), m) for m in measures if m.barline]
    wanted_values = [v for v, _ in wanted]
    measure_at = {v: m for v, m in wanted}

    limit = 10 * len(measures) + 1000
    for _ in range(limit):
        ordered = sorted(reds.values(), key=lambda r: r.time)
        timing = [lazer.TimingPoint(fmt.value(r.time), beat_length(r.bpm), r.meter, r.omit) for r in ordered]
        missing_lines, extra_lines = lazer.compare_bar_lines(timing, first_hit, last_end, wanted_values, tol)
        missing = (missing_lines[0], measure_at[missing_lines[0]]) if missing_lines else None
        extra = extra_lines[0] if extra_lines else None
        if missing is None and extra is None:
            return ordered

        if missing is not None and (extra is None or missing[0] <= extra[0]):
            m = missing[1]
            red = reds.get(m.time)
            if red is not None and not red.omit and red.meter == _meter_for(m.beats):
                raise ConversionError(f"cannot place the bar line at {fmt.value(m.time)} ms")
            if red is None:
                reds[m.time] = RedPoint(m.time, m.bpm, _meter_for(m.beats), False)
            else:
                red.omit, red.meter = False, _meter_for(m.beats)
            continue

        g, index = extra
        red = ordered[index]
        red_start = fmt.value(red.time)
        if abs(g - red_start) <= tol:
            red.omit = True
            continue
        # Skip everything up to the next wanted line (or the next red point) in one step.
        next_red = fmt.value(ordered[index + 1].time) if index + 1 < len(ordered) else 1 + last_end
        upcoming = [v for v in wanted_values if v > g + tol]
        skip_until = min(next_red, upcoming[0]) if upcoming else next_red
        split_at = [m for v, m in wanted if m.time > red.time and v < g - tol
                    and (index + 1 >= len(ordered) or m.time < ordered[index + 1].time)]
        if split_at:
            m = split_at[-1]
            meter = math.floor((max(skip_until, g) - fmt.value(m.time)) / beat_length(m.bpm) + 1e-9) + 1
            reds[m.time] = RedPoint(m.time, m.bpm, max(meter, _meter_for(m.beats)), False)
        else:
            meter = math.floor((max(skip_until, g) - red_start) / beat_length(red.bpm) + 1e-9) + 1
            red.meter = max(meter, red.meter + 1)
    raise ConversionError("could not reproduce the bar lines (too many adjustments)")


def plan_timing(events: list, fmt: TimeFormat, first_hit: float, last_end: float) -> tuple[list[RedPoint], list[GreenPoint]]:
    events = sort_events(events)
    measures = [e for e in events if isinstance(e, Measure)]
    reds = _fix_bar_lines(_plan_red_points(events), measures, fmt, first_hit, last_end)

    # Scroll speed and kiai: apply every change at a time, then attach the state to the red point
    # there (if any) and add a green point when the scroll speed differs from the red default.
    state_events: dict[Fraction, list] = {}
    for ev in events:
        if isinstance(ev, (ScrollChange, GogoChange)):
            state_events.setdefault(ev.time, []).append(ev)
    reds_at = {r.time: r for r in reds}
    greens: list[GreenPoint] = []
    scroll, kiai = 1.0, False
    for t in sorted(set(state_events) | set(reds_at)):
        changes = state_events.get(t, [])
        for ev in changes:
            if isinstance(ev, ScrollChange):
                scroll = scroll_speed(ev.value)
            else:
                kiai = ev.on
        red = reds_at.get(t)
        if red is not None:
            red.kiai = kiai
            if scroll != 1.0:
                greens.append(GreenPoint(t, scroll, kiai))
        elif changes:
            greens.append(GreenPoint(t, scroll, kiai))
    return reds, greens


def timing_point_lines(reds: list[RedPoint], greens: list[GreenPoint], fmt: TimeFormat) -> tuple[list[str], bool]:
    """Returns the [TimingPoints] lines and whether different changes had to share one time."""
    rows: dict[tuple[float, int], tuple[Fraction, str]] = {}
    merged = False

    def put(key: tuple[float, int], time: Fraction, text: str) -> None:
        nonlocal merged
        # With whole-millisecond output several exact times can share a time; the latest wins.
        if key in rows:
            merged |= rows[key][1].split(",", 1)[1] != text.split(",", 1)[1]
            if rows[key][0] > time:
                return
        rows[key] = (time, text)

    for r in reds:
        effects = (1 if r.kiai else 0) | (8 if r.omit else 0)
        put((fmt.value(r.time), 0), r.time,
            f"{fmt.text(r.time)},{lazer.format_number(beat_length(r.bpm))},{r.meter},1,0,100,1,{effects}")
    for g in greens:
        put((fmt.value(g.time), 1), g.time,
            f"{fmt.text(g.time)},{lazer.format_number(-100.0 / g.scroll)},4,1,0,100,0,{1 if g.kiai else 0}")
    return [text for _, (_, text) in sorted(rows.items())], merged


# Hit objects

def _solve_roll_length(target_ms: int, start: float, line: int, points: lazer.ControlPoints, options: OsuOptions) -> str:
    """Find a slider length whose lazer drum roll duration is exactly `target_ms`."""
    sv = points.slider_velocity_at(start)
    beat = points.timing_at(start).beat_length * (lazer.clamp(lazer.f32(100 / sv), 10, 10000) / 100.0)
    velocity = lazer.OSU_BASE_SCORING_DISTANCE * (options.slider_multiplier * lazer.VELOCITY_MULTIPLIER) / options.tick_rate * options.tick_rate
    for offset in (0.5, 0.25, 0.75, 0.1, 0.9):
        length = (target_ms + offset) * velocity / beat / lazer.VELOCITY_MULTIPLIER
        text = lazer.format_number(length)
        if lazer.drum_roll_duration(float(text), start, points, options.slider_multiplier, options.tick_rate) == target_ms:
            if float(text) > lazer.MAX_SLIDER_LENGTH:
                raise ConversionError(f"the drum roll at line {line} is too long for osu! "
                                      f"(slider length {float(text):.0f} > {lazer.MAX_SLIDER_LENGTH:.0f})")
            return text
    raise ConversionError(f"no slider length gives the {target_ms} ms drum roll at line {line}")


def _roll_duration(ev: Roll, fmt: TimeFormat) -> int:
    # lazer only supports whole-millisecond drum roll durations: use the nearest one.
    return math.floor(fmt.value(ev.end) - fmt.value(ev.time) + 0.5)


def output_end_time(ev, fmt: TimeFormat) -> float:
    """End time of a note as osu! will see it."""
    if isinstance(ev, Roll):
        return fmt.value(ev.time) + _roll_duration(ev, fmt)
    if isinstance(ev, Swell):
        return fmt.value(ev.end)
    return fmt.value(ev.time)


def hit_object_lines(events: list, fmt: TimeFormat, points: lazer.ControlPoints, options: OsuOptions) -> list[str]:
    rows: list[tuple[Fraction, str]] = []
    for ev in events:
        if isinstance(ev, Hit):
            sound = (lazer.CLAP if ev.rim else 0) | (lazer.FINISH if ev.big else 0)
            rows.append((ev.time, f"256,192,{fmt.text(ev.time)},1,{sound},0:0:0:0:"))
        elif isinstance(ev, Roll):
            start = fmt.value(ev.time)
            target = _roll_duration(ev, fmt)
            if target <= 0:
                raise ConversionError(f"the drum roll at line {ev.line} is shorter than 1 ms")
            length = _solve_roll_length(target, start, ev.line, points, options)
            sound = lazer.FINISH if ev.big else 0
            rows.append((ev.time, f"256,192,{fmt.text(ev.time)},2,{sound},L|456:192,1,{length}"))
        elif isinstance(ev, Swell):
            if ev.end <= ev.time:
                raise ConversionError(f"the balloon at line {ev.line} has no length")
            rows.append((ev.time, f"256,192,{fmt.text(ev.time)},12,0,{fmt.text(ev.end)},0:0:0:0:"))
    rows.sort(key=lambda r: r[0])
    return [text for _, text in rows]


# File

@dataclass
class Metadata:
    title: str
    title_unicode: str
    artist: str
    artist_unicode: str
    creator: str
    version: str
    source: str
    tags: str
    audio: str
    preview_time: int
    background: str | None
    video: str | None
    video_offset: int
    hp: float
    od: float


def _rounding_reorders(notes: list, reds: list[RedPoint], greens: list[GreenPoint], fmt: TimeFormat) -> bool:
    """Whether rounding puts a timing point change on the same millisecond as an earlier note."""
    latest_change: dict[float, Fraction] = {}
    for point in [*reds, *greens]:
        key = fmt.value(point.time)
        if key not in latest_change or latest_change[key] < point.time:
            latest_change[key] = point.time
    return any(n.time < latest_change.get(fmt.value(n.time), n.time) for n in notes)


@dataclass
class RenderResult:
    text: str
    # True when changes less than a millisecond apart had to share one timing point
    # (whole-millisecond output only), so scroll speed / kiai of a few notes can differ.
    merged_timing_points: bool


def render(events: list, meta: Metadata, options: OsuOptions) -> RenderResult:
    fmt = TimeFormat(options.target)
    events = sort_events(events)
    notes = [e for e in events if isinstance(e, (Hit, Roll, Swell))]
    if not notes:
        raise ConversionError("the chart has no notes")

    first_hit = fmt.value(notes[0].time)
    last_end = max(output_end_time(e, fmt) for e in notes)
    reds, greens = plan_timing(events, fmt, first_hit, last_end)
    timing, merged = timing_point_lines(reds, greens, fmt)
    merged |= _rounding_reorders(notes, reds, greens, fmt)
    points = lazer.parse_timing_points(timing)
    objects = hit_object_lines(events, fmt, points, options)

    lead_in = 0
    if first_hit < 0:
        lead_in = math.ceil(-first_hit) + 1000

    event_lines = ["//Background and Video events"]
    if meta.background:
        event_lines.append(f'0,0,"{meta.background}",0,0')
    if meta.video:
        event_lines.append(f'Video,{meta.video_offset},"{meta.video}"')
    event_lines += ["//Break Periods", "//Storyboard Layer 0 (Background)", "//Storyboard Layer 1 (Fail)",
                    "//Storyboard Layer 2 (Pass)", "//Storyboard Layer 3 (Foreground)",
                    "//Storyboard Layer 4 (Overlay)", "//Storyboard Sound Samples"]

    version = 128 if options.target == LAZER else 14
    lines = [
        f"osu file format v{version}", "",
        "[General]",
        f"AudioFilename: {meta.audio}",
        f"AudioLeadIn: {lead_in}",
        f"PreviewTime: {meta.preview_time}",
        "Countdown: 0",
        "SampleSet: Normal",
        "StackLeniency: 0.7",
        "Mode: 1",
        "LetterboxInBreaks: 0",
        "WidescreenStoryboard: 1", "",
        "[Editor]",
        "DistanceSpacing: 1",
        "BeatDivisor: 4",
        "GridSize: 32",
        "TimelineZoom: 1", "",
        "[Metadata]",
        f"Title:{meta.title}",
        f"TitleUnicode:{meta.title_unicode}",
        f"Artist:{meta.artist}",
        f"ArtistUnicode:{meta.artist_unicode}",
        f"Creator:{meta.creator}",
        f"Version:{meta.version}",
        f"Source:{meta.source}",
        f"Tags:{meta.tags}",
        "BeatmapID:0",
        "BeatmapSetID:-1", "",
        "[Difficulty]",
        f"HPDrainRate:{lazer.format_number(meta.hp)}",
        "CircleSize:5",
        f"OverallDifficulty:{lazer.format_number(meta.od)}",
        "ApproachRate:5",
        f"SliderMultiplier:{lazer.format_number(options.slider_multiplier)}",
        f"SliderTickRate:{lazer.format_number(options.tick_rate)}", "",
        "[Events]", *event_lines, "",
        "[TimingPoints]", *timing, "", "",
        "[HitObjects]", *objects, "",
    ]
    return RenderResult("\r\n".join(lines), merged)
