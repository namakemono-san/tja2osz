"""Port of the parts of osu!lazer (ppy/osu) that decide how a legacy osu!taiko beatmap plays.

Used both to choose output values (e.g. drum roll lengths) and to verify written files.
References:
  osu.Game/Beatmaps/Formats/LegacyBeatmapDecoder.cs      (timing points, difficulty)
  osu.Game/Rulesets/Objects/Legacy/ConvertHitObjectParser.cs
  osu.Game/Rulesets/Objects/Legacy/LegacyRulesetExtensions.cs (precision-adjusted beat length)
  osu.Game.Rulesets.Taiko/Beatmaps/TaikoBeatmapConverter.cs
  osu.Game/Rulesets/Objects/BarLineGenerator.cs
  osu.Game/Beatmaps/ControlPoints/*ControlPoint.cs        (value ranges)
"""
from __future__ import annotations

import bisect
import math
import struct
from dataclasses import dataclass

MIN_BEAT_LENGTH, MAX_BEAT_LENGTH = 6.0, 60000.0          # TimingControlPoint.BeatLengthBindable
MIN_SLIDER_VELOCITY, MAX_SLIDER_VELOCITY = 0.1, 10.0     # DifficultyControlPoint.SliderVelocityBindable
MIN_SCROLL_SPEED, MAX_SCROLL_SPEED = 0.01, 10.0          # EffectControlPoint.ScrollSpeedBindable
MIN_SLIDER_MULTIPLIER, MAX_SLIDER_MULTIPLIER = 0.4, 3.6  # LegacyBeatmapDecoder.applyDifficultyRestrictions
MIN_TICK_RATE, MAX_TICK_RATE = 0.5, 8.0
MAX_SLIDER_LENGTH = 131072.0                             # Parsing.MAX_COORDINATE_VALUE
DOUBLE_EPSILON = 1e-7                                    # osu.Framework Precision.DOUBLE_EPSILON

HIT_CIRCLE, SLIDER, SPINNER = 1, 2, 8
WHISTLE, FINISH, CLAP = 2, 4, 8


def f32(x: float) -> float:
    """Round a double to single precision like a C# (float) cast."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


# TaikoBeatmapConverter.VELOCITY_MULTIPLIER is the float literal 1.4f.
VELOCITY_MULTIPLIER = f32(1.4)
OSU_BASE_SCORING_DISTANCE = 100.0


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def round_away_from_zero(x: float) -> float:
    return math.copysign(math.floor(abs(x) + 0.5), x)


def format_number(x: float) -> str:
    """Shortest text that parses back to exactly the same double."""
    if x == math.floor(x) and abs(x) < 2 ** 53:
        return str(int(x))
    return repr(x)


# Control points

@dataclass
class TimingPoint:
    time: float
    beat_length: float
    meter: int
    omit_first_bar_line: bool


@dataclass
class DifficultyPoint:
    time: float
    slider_velocity: float


@dataclass
class EffectPoint:
    time: float
    kiai: bool
    scroll_speed: float


class ControlPoints:
    def __init__(self) -> None:
        self.timing: list[TimingPoint] = []
        self.difficulty: list[DifficultyPoint] = []
        self.effect: list[EffectPoint] = []

    @staticmethod
    def _at(points: list, time: float):
        index = bisect.bisect_right([p.time for p in points], time) - 1
        return points[index] if index >= 0 else None

    def timing_at(self, time: float) -> TimingPoint:
        # ControlPointInfo.TimingPointAt falls back to the first timing point.
        return self._at(self.timing, time) or (self.timing[0] if self.timing else TimingPoint(0, 1000, 4, False))

    def slider_velocity_at(self, time: float) -> float:
        point = self._at(self.difficulty, time)
        return point.slider_velocity if point else 1.0

    def effect_at(self, time: float) -> EffectPoint:
        return self._at(self.effect, time) or EffectPoint(0, False, 1.0)


def parse_timing_points(lines: list[str]) -> ControlPoints:
    """LegacyBeatmapDecoder.handleTimingPoint + flushPendingPoints.

    Lines at the same time are grouped; within a group the last inherited point decides slider
    velocity and effects, otherwise the first uninherited point does.
    """
    points = ControlPoints()
    groups: list[tuple[float, list[tuple[bool, float, int, int]]]] = []
    for line in lines:
        split = line.split(",")
        time = float(split[0].strip())
        beat_length = float(split[1].strip())
        meter = int(split[2]) if len(split) >= 3 else 4
        if meter == 0:
            meter = 4
        uninherited = split[6][0] == "1" if len(split) >= 7 else True
        effects = int(split[7]) if len(split) >= 8 else 0
        if not groups or groups[-1][0] != time:
            groups.append((time, []))
        groups[-1][1].append((uninherited, beat_length, meter, effects))

    for time, entries in groups:
        reds = [e for e in entries if e[0]]
        greens = [e for e in entries if not e[0]]
        if reds:
            _, beat_length, meter, effects = reds[0]
            points.timing.append(TimingPoint(time, clamp(beat_length, MIN_BEAT_LENGTH, MAX_BEAT_LENGTH), meter, bool(effects & 8)))
        source = greens[-1] if greens else reds[0]
        uninherited, beat_length, _, effects = source
        speed = 100.0 / -beat_length if beat_length < 0 else 1.0
        points.difficulty.append(DifficultyPoint(time, clamp(speed, MIN_SLIDER_VELOCITY, MAX_SLIDER_VELOCITY)))
        points.effect.append(EffectPoint(time, bool(effects & 1), clamp(speed, MIN_SCROLL_SPEED, MAX_SCROLL_SPEED)))
    return points


# Hit objects

def drum_roll_duration(length: float, start_time: float, points: ControlPoints,
                       slider_multiplier: float, tick_rate: float) -> int:
    """TaikoBeatmapConverter.shouldConvertSliderToHits for a native osu!taiko beatmap (one span)."""
    distance = length * VELOCITY_MULTIPLIER

    # LegacyRulesetExtensions.GetPrecisionAdjustedBeatLength (taiko)
    velocity_as_beat_length = -100 / points.slider_velocity_at(start_time)
    bpm_multiplier = clamp(f32(-velocity_as_beat_length), 10, 10000) / 100.0 if velocity_as_beat_length < 0 else 1.0
    beat_length = points.timing_at(start_time).beat_length * bpm_multiplier

    scoring_point_distance = OSU_BASE_SCORING_DISTANCE * (slider_multiplier * VELOCITY_MULTIPLIER) / tick_rate
    taiko_velocity = scoring_point_distance * tick_rate
    return int(distance / taiko_velocity * beat_length)  # C# (int) truncates toward zero


@dataclass
class TaikoObject:
    kind: str  # "hit" / "roll" / "swell"
    time: float
    end: float
    rim: bool
    big: bool


def parse_osu(text: str) -> tuple[dict[str, str], ControlPoints, list[TaikoObject]]:
    """Read the parts of a .osu file that matter for osu!taiko gameplay."""
    section = ""
    values: dict[str, str] = {}
    timing_lines: list[str] = []
    object_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section in ("General", "Metadata", "Difficulty") and ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
        elif section == "TimingPoints":
            timing_lines.append(line)
        elif section == "HitObjects":
            object_lines.append(line)

    points = parse_timing_points(timing_lines)
    slider_multiplier = clamp(float(values.get("SliderMultiplier", "1.4")), MIN_SLIDER_MULTIPLIER, MAX_SLIDER_MULTIPLIER)
    tick_rate = clamp(float(values.get("SliderTickRate", "1")), MIN_TICK_RATE, MAX_TICK_RATE)

    objects = []
    for line in object_lines:
        split = line.split(",")
        time = float(split[2])
        kind = int(split[3])
        sound = int(split[4])
        rim = bool(sound & (WHISTLE | CLAP))
        big = bool(sound & FINISH)
        if kind & SLIDER:
            length = max(0.0, float(split[7])) if len(split) > 7 else 0.0
            if length > MAX_SLIDER_LENGTH:
                raise ValueError(f"slider length {length} is above osu!'s parse limit")
            duration = drum_roll_duration(length, time, points, slider_multiplier, tick_rate)
            objects.append(TaikoObject("roll", time, time + duration, False, big))
        elif kind & SPINNER:
            end = max(time, float(split[5]))
            objects.append(TaikoObject("swell", time, end, False, False))
        else:
            objects.append(TaikoObject("hit", time, time, rim, big))
    objects.sort(key=lambda o: o.time)  # BeatmapConverter orders by start time (stable)
    return values, points, objects


# Bar lines

def generate_bar_lines_per_point(timing: list[TimingPoint], first_hit_time: float, last_object_time: float) -> list[list[float]]:
    """BarLineGenerator, grouped by the timing point that produces each line.

    Lines are generated every beatLength * meter from a timing point up to *and including* the
    next timing point's time.
    """
    result: list[list[float]] = []
    generation_start = min(0.0, first_hit_time)
    last_hit_time = 1 + last_object_time
    for i, point in enumerate(timing):
        lines: list[float] = []
        bar_length = point.beat_length * point.meter
        end_time = timing[i + 1].time if i < len(timing) - 1 else last_hit_time + bar_length
        if point.time > generation_start:
            start_time = point.time
        else:
            start_time = point.time + math.ceil((generation_start - point.time) / bar_length) * bar_length
        if point.omit_first_bar_line:
            start_time += bar_length
        t = start_time
        while end_time > t - DOUBLE_EPSILON:
            rounded = round_away_from_zero(t)
            if abs(t - rounded) <= DOUBLE_EPSILON:
                t = rounded
            lines.append(t)
            t += bar_length
        result.append(lines)
    return result


def generate_bar_lines(timing: list[TimingPoint], first_hit_time: float, last_object_time: float) -> list[float]:
    return [t for lines in generate_bar_lines_per_point(timing, first_hit_time, last_object_time) for t in lines]


def compare_bar_lines(timing: list[TimingPoint], first_hit_time: float, last_object_time: float,
                      wanted: list[float], tolerance: float) -> tuple[list[float], list[tuple[float, int]]]:
    """Compare generated bar lines with the wanted ones.

    Returns (missing wanted lines, extra generated lines with the index of their timing point).
    Wanted lines can only be checked where lazer generates bar lines at all. Extra lines are
    checked up to the last object: the generator always continues one bar past it.
    """
    per_point = generate_bar_lines_per_point(timing, first_hit_time, last_object_time)
    generated = sorted((t, i) for i, lines in enumerate(per_point) for t in lines)
    generated_times = [t for t, _ in generated]
    wanted = sorted(wanted)

    low = min(0.0, first_hit_time) + tolerance
    high = -math.inf
    if timing:
        last = timing[-1]
        high = 1 + last_object_time + last.beat_length * last.meter - tolerance

    def near(values: list[float], x: float) -> bool:
        i = bisect.bisect_left(values, x - tolerance)
        return i < len(values) and values[i] <= x + tolerance

    missing = [t for t in wanted if low <= t <= high and not near(generated_times, t)]
    extra = [(t, i) for t, i in generated if t <= 1 + last_object_time and not near(wanted, t)]
    return missing, extra
