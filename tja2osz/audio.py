"""Writes audio osu! can play, with ffmpeg.

osu! only plays MP3 and Ogg Vorbis, so other formats are converted, and the songs of a dan course
are joined into one file. Each song is placed at a whole sample (at most half a sample, ~0.01 ms,
from the exact time) and cut where the next song starts. The result is Ogg Vorbis, which has no
encoder delay, and the placement is checked afterwards by cross-correlating the output with every
source song.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from array import array
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .diagnostics import ScopedDiagnostics

CHECK_WINDOW = 4096      # samples compared per song
CHECK_MAX_LAG = 64       # samples searched around the expected position
CHECK_SEARCH_SECONDS = 60

# File extension -> codec osu! can play in it
PLAYABLE_CODECS = {".mp3": "mp3", ".ogg": "vorbis"}


@dataclass(frozen=True)
class SongPart:
    path: Path
    start_ms: Fraction  # position in the written audio
    line: int | None


def find_ffmpeg() -> tuple[str, str] | None:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    return (ffmpeg, ffprobe) if ffmpeg and ffprobe else None


def _run(args: list[str]) -> bytes:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(" / ".join(tail) or f"exit code {result.returncode}")
    return result.stdout


def _probe(ffprobe: str, path: Path) -> tuple[int, float, str]:
    """(sample rate, duration in seconds, codec name) of the first audio stream."""
    info = json.loads(_run([ffprobe, "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=sample_rate,codec_name:format=duration", "-of", "json", str(path)]))
    streams = info.get("streams") or []
    if not streams:
        raise RuntimeError(f"'{path.name}' has no audio stream")
    stream = streams[0]
    return int(stream["sample_rate"]), float(info.get("format", {}).get("duration") or 0), stream.get("codec_name", "")


def playable_by_osu(path: Path) -> bool | None:
    """Whether osu! can play the file as is; None when that cannot be checked (no ffprobe)."""
    expected = PLAYABLE_CODECS.get(path.suffix.lower())
    if expected is None:
        return False
    tools = find_ffmpeg()
    if tools is None:
        return None
    try:
        return _probe(tools[1], path)[2] == expected
    except RuntimeError:
        return None


def _decode_mono(ffmpeg: str, path: Path, rate: int, start: int, count: int) -> array:
    """Decode `count` mono float samples starting at sample `start` (after resampling to `rate`)."""
    raw = _run([ffmpeg, "-v", "error", "-i", str(path),
                "-af", f"aresample={rate},atrim=start_sample={start}:end_sample={start + count}",
                "-ac", "1", "-f", "f32le", "-"])
    samples = array("f")
    samples.frombytes(raw[:len(raw) // 4 * 4])
    return samples


def build_audio(parts: list[SongPart], output: Path, diag: ScopedDiagnostics) -> bool:
    """Write the parts into one Ogg Vorbis file (a single part at 0 ms simply converts it)."""
    tools = find_ffmpeg()
    if tools is None:
        diag.error("ffmpeg and ffprobe are needed to write the audio; install them and add them to PATH")
        return False
    ffmpeg, ffprobe = tools

    def name(i: int) -> str:
        return f"song {i + 1}" if len(parts) > 1 else f"'{parts[i].path.name}'"

    try:
        rate = _probe(ffprobe, parts[0].path)[0]
        durations = [_probe(ffprobe, p.path)[1] for p in parts]
    except RuntimeError as e:
        diag.error(f"cannot read the audio: {e}")
        return False

    starts = [math.floor(p.start_ms * rate / 1000 + Fraction(1, 2)) for p in parts]
    filters = []
    for i, part in enumerate(parts):
        skip = max(0, -starts[i])  # a song starting before the audio begins loses its head
        chain = f"[{i}:a]aresample={rate},aformat=sample_fmts=fltp:channel_layouts=stereo"
        if i + 1 < len(parts):
            length = starts[i + 1] - starts[i]
            if durations[i] * rate > length:
                diag.warning(f"the audio of {name(i)} is longer than the time until the next song and is cut", part.line)
            chain += f",atrim=start_sample={skip}:end_sample={skip + max(length, 0)}"
        elif skip:
            chain += f",atrim=start_sample={skip}"
        chain += f",asetpts=PTS-STARTPTS,adelay=delays={max(0, starts[i])}S:all=1[a{i}]"
        filters.append(chain)
    inputs = "".join(f"[a{i}]" for i in range(len(parts)))
    filters.append(f"{inputs}amix=inputs={len(parts)}:normalize=0:duration=longest[out]" if len(parts) > 1
                   else "[a0]anull[out]")

    args = [ffmpeg, "-v", "error", "-y"]
    for part in parts:
        args += ["-i", str(part.path)]
    args += ["-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "libvorbis", "-q:a", "8", str(output)]
    try:
        _run(args)
    except RuntimeError as e:
        diag.error(f"ffmpeg could not write the audio: {e}")
        return False

    for i, part in enumerate(parts):
        if starts[i] < 0:
            continue
        lag = _measure_lag(ffmpeg, part.path, output, rate, starts[i])
        if lag is None:
            diag.info(f"{name(i)} is silent at its start; its placement could not be checked", part.line)
        elif lag != 0:
            diag.error(f"{name(i)} is {lag} samples ({lag * 1000 / rate:.3f} ms) off in the written audio", part.line)
            return False
    return True


def _measure_lag(ffmpeg: str, song: Path, joined: Path, rate: int, start: int) -> int | None:
    """Offset (in samples) of the song inside the written audio, or None if nothing audible was found."""
    head = _decode_mono(ffmpeg, song, rate, 0, rate * CHECK_SEARCH_SECONDS)
    peak = max((abs(x) for x in head), default=0.0)
    if peak <= 0:
        return None
    onset = next(i for i, x in enumerate(head) if abs(x) >= peak * 0.25)
    window = head[onset:onset + CHECK_WINDOW]
    if len(window) < CHECK_WINDOW // 4:
        return None
    joined_part = _decode_mono(ffmpeg, joined, rate, start + onset - CHECK_MAX_LAG, len(window) + 2 * CHECK_MAX_LAG)
    if len(joined_part) < len(window) + 2 * CHECK_MAX_LAG:
        return None

    best_lag, best_score = 0, -math.inf
    for lag in range(-CHECK_MAX_LAG, CHECK_MAX_LAG + 1):
        offset = lag + CHECK_MAX_LAG
        score = sum(a * b for a, b in zip(window, joined_part[offset:offset + len(window)]))
        if score > best_score:
            best_lag, best_score = lag, score
    return best_lag
