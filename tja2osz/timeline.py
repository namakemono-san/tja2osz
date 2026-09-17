"""Timeline events produced by the TJA parser and consumed by the osu! writer and verifier.

All times are exact ``Fraction`` milliseconds relative to the start of the audio file.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Measure:
    """Start of a measure (where TJA simulators place a bar line)."""
    time: Fraction
    bpm: Fraction
    beats: Fraction  # length in quarter-note beats: 4 * numerator / denominator
    barline: bool
    line: int


@dataclass(frozen=True)
class BpmChange:
    """A #BPMCHANGE in the middle of a measure (changes at a measure start live in Measure)."""
    time: Fraction
    bpm: Fraction
    line: int


@dataclass(frozen=True)
class ScrollChange:
    time: Fraction
    value: Fraction
    line: int


@dataclass(frozen=True)
class GogoChange:
    time: Fraction
    on: bool
    line: int


@dataclass(frozen=True)
class Hit:
    time: Fraction
    rim: bool
    big: bool
    bpm: Fraction
    scroll: Fraction
    line: int


@dataclass(frozen=True)
class Roll:
    time: Fraction
    end: Fraction
    big: bool
    bpm: Fraction
    scroll: Fraction
    line: int


@dataclass(frozen=True)
class Swell:
    time: Fraction
    end: Fraction
    bpm: Fraction
    scroll: Fraction
    line: int


NOTE_TYPES = (Hit, Roll, Swell)


def sort_events(events: list) -> list:
    # Stable sort: events at the same time keep the order in which the chart defined them.
    return sorted(events, key=lambda e: e.time)
