"""TJA reading, validation and chart notation parsing.

Timing follows TJAPlayer3 / OpenTaiko (OpenTaiko/src/Songs/CTja.cs):

* A measure lasts ``240000 / BPM * numerator / denominator`` ms and is split evenly between
  every note symbol (ASCII letter or digit) written before its comma.
* Commands apply at the current position. ``#BPMCHANGE`` / ``#MEASURE`` inside a measure only
  affect the symbols that follow.
* A measure's bar line is placed when its first note line is read, using the #BARLINEOFF state
  at that moment.
* Each branch section (#N / #E / #M) starts from the state at #BRANCHSTART. After the branch,
  the chart continues from the state at the end of the *last defined* section.
* A non-roll note inside an unfinished roll ends the roll; repeated roll heads are blanks.
* OFFSET is the chart start relative to the audio, negated (OFFSET:-1.5 = chart starts at 1.5 s).

The simulators truncate times to whole milliseconds; here every time is an exact Fraction so
that nothing is rounded before the osu! output stage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from fractions import Fraction

from .diagnostics import Diagnostics, ScopedDiagnostics
from .timeline import BpmChange, GogoChange, Hit, Measure, Roll, ScrollChange, Swell

COURSE_ALIASES = {
    "0": "easy", "easy": "easy",
    "1": "normal", "normal": "normal",
    "2": "hard", "hard": "hard",
    "3": "oni", "oni": "oni",
    "4": "edit", "edit": "edit", "ura": "edit",
    "5": "tower", "tower": "tower",
    "6": "dan", "dan": "dan",
}

# Note symbol kinds
EMPTY, DON, KA, DON_BIG, KA_BIG, ROLL, ROLL_BIG, BALLOON, ROLL_END, UNSUPPORTED = range(10)

# OpenTaiko NotesManager.CharToNoteType (symbols are case-sensitive)
NOTE_SYMBOLS = {
    "0": EMPTY,
    "1": DON, "2": KA, "3": DON_BIG, "4": KA_BIG,
    "5": ROLL, "6": ROLL_BIG, "7": BALLOON, "8": ROLL_END, "9": BALLOON,
    "A": DON_BIG,   # joint big don (2P)
    "B": KA_BIG,    # joint big ka (2P)
    "C": UNSUPPORTED,
    "D": BALLOON,   # fuse roll
    "F": UNSUPPORTED,
    "G": UNSUPPORTED,
    "H": ROLL_BIG,
    "I": ROLL,
}
UNSUPPORTED_NAMES = {"C": "bomb", "F": "ADLIB", "G": "kadon"}
HIT_KINDS = {DON: (False, False), KA: (True, False), DON_BIG: (False, True), KA_BIG: (True, True)}
ROLL_HEADS = {ROLL, ROLL_BIG, BALLOON}

BRANCH_SECTIONS = ("N", "E", "M")
BRANCH_FALLBACK = {"M": "MEN", "E": "ENM", "N": "NEM"}

# Commands that do not affect an osu!taiko conversion once a branch has been chosen.
IGNORED_COMMANDS = {"SECTION", "LEVELHOLD", "LYRIC", "NMSCROLL"}
# Commands that change how a chart looks or plays but have no osu!taiko equivalent.
UNSUPPORTED_COMMANDS = {
    "BMSCROLL", "HBSCROLL", "DIRECTION", "SUDDEN", "JPOSSCROLL", "SENOTECHANGE", "BARLINE",
    "SPLITLANE", "MERGELANE", "GAMETYPE", "PARTNERNOTE",
}

UTF8_BOM = b"\xef\xbb\xbf"

# Time from #NEXTSONG to the start of that song in a dan course: CTja.msDanNextSongDelay (6200 ms)
# plus OpenTaiko's default music pre-time (ConfigIni.MusicPreTimeMs 2500 + MusicPreTimeMsOffset 1500).
DAN_SONG_GAP_MS = Fraction(6200 + 2500 + 1500)

_COMMENT_RE = re.compile(r"(?<!:)//.*")  # keep "://" in URLs (e.g. MAKER)
_HEADER_RE = re.compile(r"^([A-Za-z0-9_]+)\s*:(.*)$")
_COMMAND_RE = re.compile(r"^#([A-Za-z0-9_]+)\s*(.*)$")
_UNSIGNED = r"(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_REAL_RE = re.compile(rf"[-+]?{_UNSIGNED}")
_COMPLEX_RE = re.compile(rf"([-+]?{_UNSIGNED})?(?:([-+](?:{_UNSIGNED})?)i)?")
_IMAGINARY_RE = re.compile(rf"[-+]?(?:{_UNSIGNED})?i")


def check_bpm_range(bpm: Fraction, diag: ScopedDiagnostics, line: int | None = None) -> None:
    # osu! clamps beat lengths to 6-60000 ms (TimingControlPoint.BeatLengthBindable).
    if not 1 <= bpm <= 10000:
        diag.warning(f"BPM {float(bpm):g} is outside osu!'s 1-10000 range; scroll speed and bar lines use the clamped value", line)


def split_escaped_commas(text: str) -> list[str]:
    """Split on commas, keeping "\\," as a literal comma (#NEXTSONG arguments)."""
    return [part.replace("\\,", ",") for part in re.split(r"(?<!\\),", text)]


def parse_real(text: str) -> Fraction | None:
    s = text.strip()
    if not _REAL_RE.fullmatch(s):
        return None
    return Fraction(s)


def parse_scroll(text: str) -> tuple[Fraction, bool] | None:
    """Parse #SCROLL, including OpenTaiko's complex form ("1+2i"). Returns (real part, has imaginary part)."""
    s = text.replace(" ", "")
    if _IMAGINARY_RE.fullmatch(s):
        return Fraction(0), True
    m = _COMPLEX_RE.fullmatch(s)
    if not m or m.group(1) is None:
        return None
    return Fraction(m.group(1)), m.group(2) is not None


def decode_text(data: bytes, encoding: str | None, diag: Diagnostics) -> str:
    if encoding:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as e:
            diag.error(f"the file is not valid {encoding}: {e}")
            return data.decode(encoding, errors="replace")
    if data.startswith(UTF8_BOM):
        try:
            return data[len(UTF8_BOM):].decode("utf-8")
        except UnicodeDecodeError as e:
            diag.error(f"the file has a UTF-8 BOM but is not valid UTF-8: {e}")
            return data[len(UTF8_BOM):].decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp932")
    except UnicodeDecodeError as e:
        diag.error(f"the file is neither valid UTF-8 nor Shift-JIS: {e}")
        return data.decode("cp932", errors="replace")


# File structure

@dataclass
class ChartSource:
    headers: dict[str, str]  # headers defined before #START (later courses inherit earlier values)
    body: list[tuple[int, str]]  # (line number, comment-stripped line)
    player: str | None  # "P1" / "P2" for #START P1 / #START P2
    start_line: int

    @property
    def course_value(self) -> str:
        return self.headers.get("COURSE", "Oni").strip()

    @property
    def course(self) -> str | None:
        return COURSE_ALIASES.get(self.course_value.lower())


def _command_name(line: str) -> str | None:
    m = _COMMAND_RE.match(line)
    return m.group(1).upper() if m else None


def split_charts(text: str, diag: Diagnostics) -> list[ChartSource]:
    headers: dict[str, str] = {}
    charts: list[ChartSource] = []
    body: list[tuple[int, str]] | None = None
    player: str | None = None
    start_line = 0

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _COMMENT_RE.sub("", raw).strip()
        if not line:
            continue
        name = _command_name(line)
        if body is not None:
            if name == "END":
                charts.append(ChartSource(dict(headers), body, player, start_line))
                body = None
            elif name == "START":
                diag.error(f"#START inside the chart starting at line {start_line}, which was not closed with #END", lineno)
            else:
                body.append((lineno, line))
            continue
        if name == "START":
            arg = line[len("#START"):].strip().upper()
            player = arg if arg in ("P1", "P2") else None
            if arg and player is None:
                diag.warning(f"unknown #START argument '{arg}' is ignored", lineno)
            body = []
            start_line = lineno
            continue
        if name == "END":
            diag.warning("#END without #START is ignored", lineno)
            continue
        m = _HEADER_RE.match(line)
        if m:
            headers[m.group(1).upper()] = m.group(2).strip()
            continue
        diag.warning(f"unrecognized line is ignored: {line[:40]}", lineno)

    if body is not None:
        diag.error(f"the chart starting at line {start_line} is not closed with #END", start_line)
        charts.append(ChartSource(dict(headers), body, player, start_line))
    if not charts:
        diag.error("no chart found (#START ... #END)")
    return charts


# Notation

@dataclass(frozen=True)
class _Command:
    name: str
    arg: str
    line: int


@dataclass(frozen=True)
class _Note:
    symbol: str
    line: int


@dataclass(frozen=True)
class _OpenRoll:
    time: Fraction
    kind: int
    big: bool
    bpm: Fraction
    scroll: Fraction
    line: int


@dataclass
class _State:
    time: Fraction
    bpm: Fraction
    num: Fraction
    den: Fraction
    scroll: Fraction
    barline: bool
    roll: _OpenRoll | None


@dataclass(frozen=True)
class DanSong:
    title: str
    subtitle: str
    genre: str
    wave: str  # audio file relative to the .tja
    audio_start: Fraction  # timeline time (ms) at which the song's audio starts
    line: int


@dataclass
class _BranchBlock:
    line: int
    start: _State
    sections: dict[str, list] = field(default_factory=dict)
    ends: dict[str, _State] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)  # definition order
    current: str | None = None


class NotationParser:
    """Turns the body of one chart into timeline events for a single branch route."""

    def __init__(self, headers: dict[str, str], branch: str, diag: ScopedDiagnostics, dan: bool = False):
        self.diag = diag
        self.branch = branch
        self.dan = dan
        self.dan_songs: list[DanSong] = []

        bpm = Fraction(120)
        if headers.get("BPM", "").strip():
            parsed = parse_real(headers["BPM"])
            if parsed is None or parsed <= 0:
                diag.error(f"BPM must be a positive number (got '{headers['BPM']}')")
            else:
                bpm = parsed
                check_bpm_range(bpm, diag)
        offset = Fraction(0)
        if headers.get("OFFSET", "").strip():
            parsed = parse_real(headers["OFFSET"])
            if parsed is None:
                diag.error(f"OFFSET must be a number (got '{headers['OFFSET']}')")
            else:
                offset = parsed

        # Timeline times are relative to the audio: a chart time t is at t - OFFSET.
        self.offset_ms = offset * 1000
        self.state = _State(-self.offset_ms, bpm, Fraction(4), Fraction(4), Fraction(1), True, None)
        self.events: list = []
        self.target: list = self.events
        self.block: _BranchBlock | None = None
        # Set when later parts of the chart can overlap earlier ones (negative #DELAY, or a chosen
        # branch section that is longer than the last defined one).
        self.overlaps = False

    # Entry point

    def run(self, body: list[tuple[int, str]]) -> list:
        items: list = []
        last_line = body[-1][0] if body else 0
        for lineno, line in body:
            if line.startswith("#"):
                m = _COMMAND_RE.match(line)
                if not m:
                    self.diag.error(f"malformed command '{line}'", lineno)
                    continue
                items.append(_Command(m.group(1).upper(), m.group(2).strip(), lineno))
                continue
            if not line[0].isdigit() and ":" in line:
                # OpenTaiko treats such lines as headers, not note data.
                self.diag.warning(f"header '{line.split(':', 1)[0]}' inside the chart body is ignored", lineno)
                continue
            comma = line.find(",")
            if comma != -1 and line[comma + 1:].strip():
                self.diag.error("more than one measure on a single line (simulators read this differently)", lineno)
            for ch in line:
                if ch == ",":
                    self._measure(items, lineno, advance=True)
                    items = []
                elif ch.isspace():
                    continue
                elif ch.isascii() and ch.isalnum():
                    items.append(_Note(ch, lineno))
                else:
                    self.diag.error(f"invalid character '{ch}' in note data", lineno)

        if any(isinstance(i, _Note) for i in items):
            self.diag.warning("the last measure is not terminated with ','", last_line)
            self._measure(items, last_line, advance=True)
        elif items:
            self._measure(items, last_line, advance=False)

        self._close_branch_block()
        roll = self.state.roll
        if roll is not None:
            self.diag.error("roll/balloon is not closed with '8' before #END", roll.line)
            self._close_roll(self.state.time)
        return self.events

    def _emit(self, event) -> None:
        self.target.append(event)

    # Measures

    def _measure_length(self) -> Fraction:
        st = self.state
        return Fraction(240000) / st.bpm * st.num / st.den

    def _measure(self, items: list, line: int, advance: bool) -> None:
        count = sum(1 for i in items if isinstance(i, _Note))
        started = False
        for item in items:
            if isinstance(item, _Command):
                self._command(item, mid_measure=started)
                continue
            if not started:
                self._emit_measure(item.line)
                started = True
            self._note(item)
            self.state.time += self._measure_length() / count
        if advance and not started:
            self._emit_measure(line)
            self.state.time += self._measure_length()

    def _emit_measure(self, line: int) -> None:
        st = self.state
        self._emit(Measure(st.time, st.bpm, 4 * st.num / st.den, st.barline, line))

    # Commands

    def _command(self, cmd: _Command, mid_measure: bool) -> None:
        name, arg, line = cmd.name, cmd.arg, cmd.line
        st = self.state
        if name == "BPMCHANGE":
            bpm = parse_real(arg)
            if bpm is None or bpm <= 0:
                self.diag.error(f"#BPMCHANGE needs a positive number (got '{arg}')", line)
                return
            check_bpm_range(bpm, self.diag, line)
            st.bpm = bpm
            if mid_measure:
                self._emit(BpmChange(st.time, bpm, line))
        elif name == "MEASURE":
            parts = arg.split("/")
            num = parse_real(parts[0]) if len(parts) == 2 else None
            den = parse_real(parts[1]) if len(parts) == 2 else None
            if num is None or den is None or num <= 0 or den <= 0:
                self.diag.error(f"#MEASURE needs 'numerator/denominator' with positive numbers (got '{arg}')", line)
                return
            st.num, st.den = num, den
        elif name == "DELAY":
            delay = parse_real(arg)
            if delay is None:
                self.diag.error(f"#DELAY needs a number (got '{arg}')", line)
                return
            if delay < 0:
                self.diag.warning("negative #DELAY moves the following notes backwards", line)
                self.overlaps = True
            st.time += delay * 1000
        elif name == "SCROLL":
            parsed = parse_scroll(arg)
            if parsed is None:
                self.diag.error(f"#SCROLL needs a number (got '{arg}')", line)
                return
            value, has_imaginary = parsed
            if has_imaginary:
                self.diag.warning("the vertical part of a complex #SCROLL is ignored", line)
            if value <= 0:
                self.diag.warning(f"#SCROLL {arg}: zero or negative scroll cannot be represented; its absolute value is used (minimum 0.01)", line)
            elif not Fraction(1, 100) <= value <= 10:
                self.diag.warning(f"#SCROLL {arg} is outside osu!'s 0.01-10 range and will be clamped", line)
            st.scroll = value
            self._emit(ScrollChange(st.time, value, line))
        elif name in ("GOGOSTART", "GOGOEND"):
            self._emit(GogoChange(st.time, name == "GOGOSTART", line))
        elif name in ("BARLINEOFF", "BARLINEON"):
            st.barline = name == "BARLINEON"
        elif name == "BRANCHSTART":
            self._close_branch_block()
            self.block = _BranchBlock(line, replace(st))
        elif name in BRANCH_SECTIONS:
            self._switch_branch(name, line)
        elif name == "BRANCHEND":
            if self.block is None:
                self.diag.warning("#BRANCHEND without #BRANCHSTART is ignored", line)
            self._close_branch_block()
        elif name in IGNORED_COMMANDS:
            pass
        elif name in UNSUPPORTED_COMMANDS:
            self.diag.warning(f"#{name} has no osu!taiko equivalent and is ignored", line)
        elif name == "NEXTSONG":
            self._next_song(arg, line)
        else:
            self.diag.warning(f"unknown command #{name} is ignored", line)

    def _next_song(self, arg: str, line: int) -> None:
        """#NEXTSONG title,subtitle,genre,audio[,scoreinit,scorediff,level,course,showtitle]"""
        if not self.dan:
            self.diag.error("#NEXTSONG is only allowed in dan courses (COURSE:Dan)", line)
            return
        self._close_branch_block(forced=True)
        parts = [p.strip() for p in split_escaped_commas(arg)]
        wave = parts[3] if len(parts) > 3 else ""
        if not wave:
            self.diag.error("#NEXTSONG needs 'title,subtitle,genre,audio file'", line)
            return
        if self.state.roll is not None:
            self.diag.error("roll/balloon is not closed with '8' before #NEXTSONG", self.state.roll.line)
            self._close_roll(self.state.time)
        self.state.time += DAN_SONG_GAP_MS
        # OpenTaiko starts the song's audio at the chart time of #NEXTSONG plus the gap; OFFSET then
        # shifts the notes against it exactly as in a normal chart.
        self.dan_songs.append(DanSong(parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "",
                                      wave.replace("\\", "/"), self.state.time + self.offset_ms, line))

    # Branches

    def _switch_branch(self, name: str, line: int) -> None:
        block = self.block
        if block is None:
            self.diag.error(f"#{name} without #BRANCHSTART", line)
            return
        if name in block.sections:
            self.diag.error(f"branch section #{name} is defined twice in the branch starting at line {block.line}", line)
            block.order.remove(name)
        if block.current is not None:
            block.ends[block.current] = replace(self.state)
        self.state = replace(block.start)
        block.current = name
        block.sections.setdefault(name, [])
        block.order.append(name)
        self.target = block.sections[name]

    def _close_branch_block(self, forced: bool = False) -> None:
        """End the current branch block.

        forced (at #NEXTSONG): OpenTaiko continues from the end of the longest section instead of
        the last defined one (CTja.GotoBranchEnd(forced: true)).
        """
        block = self.block
        if block is None:
            return
        self.block = None
        self.target = self.events
        if block.current is None:
            return  # #BRANCHSTART without sections: everything went to the common timeline
        block.ends[block.current] = replace(self.state)

        pick = next(b for b in BRANCH_FALLBACK[self.branch] if b in block.sections)
        if pick != self.branch:
            self.diag.info(f"the branch has no #{self.branch} section; #{pick} is used", block.line)
        self.events.extend(block.sections[pick])

        last = block.order[-1]
        picked_end, last_end = block.ends[pick], block.ends[last]
        if forced:
            last_end = replace(last_end, time=max(e.time for e in block.ends.values()))
        signatures = {(e.time, e.bpm, e.num, e.den, e.scroll, e.barline) for e in block.ends.values()}
        if len(signatures) > 1:
            self.diag.warning(
                "branch sections end in different states; the chart continues from the last defined "
                f"section (#{last}) like TJAPlayer3/OpenTaiko (TaikoJiro may differ)", block.line)

        if picked_end.time > last_end.time:
            self.overlaps = True

        # Rolls are tracked per branch, everything else continues from the last defined section.
        self.state = replace(last_end, roll=picked_end.roll)
        if picked_end.scroll != last_end.scroll:
            self.events.append(ScrollChange(last_end.time, last_end.scroll, block.line))

    # Notes

    def _note(self, note: _Note) -> None:
        symbol, line = note.symbol, note.line
        kind = NOTE_SYMBOLS.get(symbol)
        if kind is None:
            self.diag.error(f"unknown note symbol '{symbol}'", line)
            kind = UNSUPPORTED
        if kind == EMPTY:
            return

        st = self.state
        if st.roll is not None:
            if kind in ROLL_HEADS:
                return  # repeated roll head (e.g. 999998) is a blank
            if kind == ROLL_END:
                self._close_roll(st.time)
                return
            self.diag.warning(f"roll/balloon starting at line {st.roll.line} is ended by '{symbol}' instead of '8'", line)
            self._close_roll(st.time)
        elif kind == ROLL_END:
            self.diag.warning("'8' without an open roll/balloon is ignored", line)
            return

        if kind in HIT_KINDS:
            rim, big = HIT_KINDS[kind]
            self._emit(Hit(st.time, rim, big, st.bpm, st.scroll, line))
        elif kind in ROLL_HEADS:
            st.roll = _OpenRoll(st.time, kind, kind == ROLL_BIG, st.bpm, st.scroll, line)
        elif symbol in UNSUPPORTED_NAMES:
            self.diag.warning(f"note '{symbol}' ({UNSUPPORTED_NAMES[symbol]}) has no osu!taiko equivalent and is dropped", line)

    def _close_roll(self, end: Fraction) -> None:
        roll = self.state.roll
        self.state.roll = None
        if roll.kind == BALLOON:
            self._emit(Swell(roll.time, end, roll.bpm, roll.scroll, roll.line))
        else:
            self._emit(Roll(roll.time, end, roll.big, roll.bpm, roll.scroll, roll.line))
