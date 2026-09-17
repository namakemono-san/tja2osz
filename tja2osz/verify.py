"""Re-reads a written .osu the way osu!lazer does and compares it with the TJA timeline."""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from . import lazer
from .osu import LAZER, beat_length, scroll_speed
from .timeline import GogoChange, Hit, Measure, Roll, Swell, sort_events


@dataclass
class VerifyResult:
    notes: int = 0
    bar_lines: int = 0
    max_note_error: float = 0.0  # ms, note start times
    max_end_error: float = 0.0   # ms, drum roll / balloon ends
    timing_problems: list[str] = field(default_factory=list)      # notes at the wrong time or of the wrong type
    appearance_problems: list[str] = field(default_factory=list)  # scroll speed, kiai, bar lines


def _kind(event) -> tuple[str, bool, bool]:
    if isinstance(event, Hit):
        return "hit", event.rim, event.big
    if isinstance(event, Roll):
        return "roll", False, event.big
    return "swell", False, False


def verify(text: str, events: list, target: str, max_problems: int = 20) -> VerifyResult:
    result = VerifyResult()
    exact = target == LAZER
    note_tolerance = 1e-6 if exact else 0.5 + 1e-9
    roll_end_tolerance = 0.5 + 1e-6

    def add(problems: list[str], message: str) -> None:
        if len(problems) < max_problems:
            problems.append(message)

    try:
        _, points, objects = lazer.parse_osu(text)
    except (ValueError, IndexError) as e:
        add(result.timing_problems, f"osu! could not read the file: {e}")
        return result

    events = sort_events(events)
    expected = [e for e in events if isinstance(e, (Hit, Roll, Swell))]
    gogo_times = [e.time for e in events if isinstance(e, GogoChange)]
    gogo_states = [e.on for e in events if isinstance(e, GogoChange)]
    result.notes = len(objects)

    if len(objects) != len(expected):
        add(result.timing_problems, f"object count differs: {len(objects)} in .osu, {len(expected)} in TJA")

    for exp, obj in zip(expected, objects):
        where = f"TJA line {exp.line}"
        kind, rim, big = _kind(exp)
        if (obj.kind, obj.rim, obj.big) != (kind, rim, big):
            add(result.timing_problems, f"{where}: expected {kind} (rim={rim}, big={big}), got {obj.kind} (rim={obj.rim}, big={obj.big})")

        error = abs(obj.time - float(exp.time))
        result.max_note_error = max(result.max_note_error, error)
        if error > note_tolerance:
            add(result.timing_problems, f"{where}: time is off by {error:.6f} ms")

        if isinstance(exp, (Roll, Swell)):
            end_error = abs(obj.end - float(exp.end))
            result.max_end_error = max(result.max_end_error, end_error)
            if end_error > (roll_end_tolerance if isinstance(exp, Roll) else note_tolerance):
                add(result.timing_problems, f"{where}: end is off by {end_error:.6f} ms")

        effect = points.effect_at(obj.time)
        want_scroll = scroll_speed(exp.scroll)
        if abs(effect.scroll_speed - want_scroll) > 1e-9 * want_scroll:
            add(result.appearance_problems, f"{where}: scroll speed {effect.scroll_speed} instead of {want_scroll}")
        want_beat = beat_length(exp.bpm)
        got_beat = points.timing_at(obj.time).beat_length
        if abs(got_beat - want_beat) > 1e-9 * want_beat:
            add(result.appearance_problems, f"{where}: BPM {60000 / got_beat:g} instead of {60000 / want_beat:g}")
        index = bisect.bisect_right(gogo_times, exp.time) - 1
        want_kiai = gogo_states[index] if index >= 0 else False
        if effect.kiai != want_kiai:
            add(result.appearance_problems, f"{where}: kiai is {'on' if effect.kiai else 'off'} but go-go time is {'on' if want_kiai else 'off'}")

    if objects:
        tolerance = 1e-4 if exact else 1.0 + 1e-6
        first_hit = objects[0].time
        last_end = max(o.end for o in objects)
        wanted = [float(e.time) for e in events if isinstance(e, Measure) and e.barline]
        missing, extra = lazer.compare_bar_lines(points.timing, first_hit, last_end, wanted, tolerance)
        result.bar_lines = len(lazer.generate_bar_lines(points.timing, first_hit, last_end))
        for t in missing:
            add(result.appearance_problems, f"missing bar line at {t:.4f} ms")
        for t, _ in extra:
            add(result.appearance_problems, f"unexpected bar line at {t:.4f} ms")
    return result
