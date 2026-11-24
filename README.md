# tja2osz

Convert TJA (Taiko Jiro / TJAPlayer / OpenTaiko) charts into osu!taiko beatmaps (`.osz`).
The [`osz2tja`](#osz2tja-osutaiko--tja) command converts the other way, so charts can be made in
the osu! editor and played in Taiko-san Jiro.

- Every difficulty in a `.tja` becomes its own osu! difficulty in one `.osz`
- Note times are exact: every written file is read back the way osu!lazer reads it and compared with the TJA
- The TJA is validated first; broken charts are not converted
- Dan courses (段位道場) are joined into a single beatmap
- Audio osu! cannot play (anything but MP3 / Ogg Vorbis) is converted to Ogg Vorbis without shifting it
- No dependencies besides Python (ffmpeg only for audio conversion and dan courses)

## Demo

[![tja2osz demo video](https://i.ytimg.com/vi/69VvixENSXE/hqdefault.jpg)](https://youtu.be/69VvixENSXE)

## Requirements

- Python 3.9 or newer (tested with 3.12)
- [ffmpeg](https://ffmpeg.org/) and ffprobe on `PATH`, only for dan courses and audio that is not MP3 / Ogg Vorbis

## Install

```sh
pip install .
```

Or run it from a checkout without installing:

```sh
python -m tja2osz --help
```

## Usage

```sh
tja2osz song.tja
tja2osz charts/ -o out
tja2osz song.tja --target stable --branch E --courses oni,edit
```

Folders are searched recursively. The `.osz` is written next to each `.tja` unless `-o` is given.
The audio file (`WAVE:`), background (`BGIMAGE:`) and video (`BGMOVIE:`) must sit next to the `.tja`.

osu! only plays MP3 and Ogg Vorbis. Other audio (WAV, FLAC, Opus in `.ogg`, ...) is converted to Ogg
Vorbis with ffmpeg, and the converted file is checked against the original so that it is not shifted.

| Option | Description |
| :-- | :-- |
| `-o`, `--output` | Output folder |
| `--target lazer\|stable` | `lazer` (default): fractional-millisecond times. `stable`: whole milliseconds for osu!stable |
| `--branch N\|E\|M` | Branch route to convert (default `M`; falls back to the nearest existing section) |
| `--courses` | Courses to convert: `easy,normal,hard,oni,edit,tower,dan` |
| `--folder` / `--no-osz` | Also / only write an extracted beatmap folder |
| `--title`, `--artist` | Romanised (ASCII) title / artist |
| `--artist-unicode`, `--creator`, `--source` | Other metadata |
| `--hp`, `--od` | HP drain rate / overall difficulty for every difficulty |
| `--slider-multiplier`, `--tick-rate` | Base scroll speed (default 1.4) / slider tick rate (default 4) |
| `--strict` | Treat warnings as errors |
| `--force` | Write output even when there are errors |
| `--encoding` | Force the TJA encoding (otherwise BOM → UTF-8 → Shift-JIS) |
| `-v`, `--verbose` | Also show informational messages |

Exit status: `0` all files converted, `1` bad arguments or no input, `2` some files failed.

## How charts are mapped

| TJA | osu!taiko |
| :-- | :-- |
| `COURSE` Easy / Normal / Hard / Oni / Edit / Tower / Dan | Kantan / Futsuu / Muzukashii / Oni / Inner Oni / Tower / Dan |
| `TITLEEN` or `TITLE`, `TITLEJA` or `TITLE` | `Title` (ASCII only), `TitleUnicode` |
| `SUBTITLEEN` or `SUBTITLE`, `SUBTITLEJA` or `SUBTITLE` | `Artist` (ASCII only), `ArtistUnicode` (a `from ...` subtitle becomes `Source`) |
| `MAKER`, `GENRE`, `DEMOSTART` | `Creator`, `Tags`, `PreviewTime` |
| `BPM`, `#BPMCHANGE`, `#MEASURE` | Uninherited timing points |
| `#SCROLL` | Inherited timing points |
| `#GOGOSTART` / `#GOGOEND` | Kiai |
| Bar lines, `#BARLINEOFF` | Timing points and meters chosen so osu! draws exactly the TJA bar lines |
| `1 2 3 4 A B` | Don / ka / big notes |
| `5 6 H I` … `8` | Drum rolls |
| `7 9 D` … `8` | Swells |

Timing follows TJAPlayer3 / OpenTaiko: branch sections start from the `#BRANCHSTART` state and the
chart continues from the last defined section; a note inside an unfinished roll ends the roll.

osu!'s `Title` and `Artist` accept printable ASCII only. When no ASCII value is available the
non-ASCII characters are removed (as the osu!lazer editor does) with a warning.

## Accuracy

| Target | File format | Note times | Drum roll ends |
| :-- | :-- | :-- | :-- |
| `lazer` | v128 | exact | within 0.5 ms (osu! uses whole-millisecond roll lengths) |
| `stable` | v14 | within 0.5 ms | within 0.5 ms |

Each conversion reports the largest difference it measured. Scroll speed, BPM, kiai and bar lines
are checked as well.

## Dan courses

The songs of a `COURSE:Dan` chart are joined with ffmpeg into one Ogg Vorbis file, each placed at
the sample where OpenTaiko starts it (10.2 s after `#NEXTSONG`). The joined audio is checked by
cross-correlation against every source song.

## Limitations

These are reported as warnings:

- Only one branch route can be converted
- Balloon hit counts are decided by osu! (from OD and length), not by `BALLOON`
- `EXAM` pass conditions of dan courses are ignored
- Bombs (`C`), ADLIB (`F`) and kadon (`G`) notes are dropped
- `#SCROLL` outside 0.01–10 and BPM outside 1–10000 are clamped; negative scroll loses its direction
- `#BMSCROLL`, `#HBSCROLL`, `#DIRECTION`, `#SUDDEN`, `#JPOSSCROLL` and similar commands are ignored
- Parts of a chart that overlap in time (negative `#DELAY`, branch sections of different lengths) share one scroll speed and bar line timeline in osu!

## osz2tja: osu!taiko → TJA

Map in the osu! editor (osu!taiko mode), convert, then fine-tune TJA-only things (balloon counts,
branches, LEVEL) in a TJA editor.

```sh
osz2tja "song.osz"
osz2tja "C:/osu!/Songs/123 Artist - Title" -o out --level oni=8
osz2tja song.osz --course "Insane=oni" --course "Extra=edit" --audio ogg
```

Inputs are `.osz` files, `.osu` files or folders (searched recursively for both; a folder of
`.osu` files is one beatmap set). Each set becomes a song folder in `-o` (default: the current
folder) with one `.tja` holding every osu!taiko difficulty, plus the audio.

| Option | Description |
| :-- | :-- |
| `-o`, `--output` | Folder to write the song folders into |
| `--course DIFFICULTY=COURSE` | Course for a difficulty (`easy normal hard oni edit`); repeatable |
| `--level COURSE=N` | `LEVEL` of a course (default: Easy 3, Normal 5, Hard 7, Oni 8, Edit 9) |
| `--snap-tolerance MS` | How far a note may be from its snap (default 2 ms) |
| `--offset MS` | Move every note by this many ms (positive = later), e.g. after converting the audio |
| `--encoding` | `cp932` (Shift-JIS, Taiko-san Jiro; default) or `utf-8` (with BOM, TJAPlayer3 / OpenTaiko) |
| `--ignore-slider-multiplier` | Write slider velocity as `#SCROLL` without scaling by `SliderMultiplier / 1.4` |
| `--audio copy\|ogg` | Copy the audio (default) or convert it to Ogg Vorbis with ffmpeg |
| `--force` | Write output even when there are errors |

### How beatmaps are mapped

| osu!taiko | TJA |
| :-- | :-- |
| Difficulty name (Kantan / Futsuu / Muzukashii / Oni / Inner Oni, Easy / Normal / Hard, ...) | `COURSE` (others go to the free courses by note count) |
| `TitleUnicode` / `Title`, `ArtistUnicode` / `Artist`, `Creator`, `PreviewTime` | `TITLE` / `TITLEEN`, `SUBTITLE` / `SUBTITLEEN`, `MAKER`, `DEMOSTART` |
| Red lines (BPM, meter) | `BPM`, `#BPMCHANGE`, `#MEASURE` |
| Red line in the middle of a measure | That measure is shortened with `#MEASURE`; a remainder that no fraction fits becomes `#DELAY` |
| Slider velocity | `#SCROLL` (× `SliderMultiplier / 1.4`) |
| Kiai | `#GOGOSTART` / `#GOGOEND` |
| Bar lines as osu! draws them (incl. "omit first bar line") | `#BARLINEOFF` / `#BARLINEON` |
| Circles (whistle / clap = ka, finish = big) | `1 2 3 4` |
| Sliders (finish = big) | `5` / `6` … `8` |
| Spinners | `7` … `8`, `BALLOON` from osu!lazer's hit count formula |

Every note is put on the simplest beat snap within the tolerance (1/1 … 1/16, 1/5, 1/7, 1/9, then
1/24 … 1/48), and each measure gets the division its notes need. The written `.tja` is read back
with the TJA parser and compared with the beatmap note by note.

### Accuracy

- Notes keep their osu! time up to the snap: beatmaps with fractional times (osu!lazer, or
  `tja2osz` output) convert exactly; osu!stable's whole-millisecond times are off their snap by
  up to 1 ms, and that is what remains
- A measure that ends a fraction of a millisecond before the next red line (whole-ms red lines)
  is played that much faster, so the following red line stays exact

### Limitations

These are reported as warnings:

- Notes off every snap are moved to the nearest grid position
- Objects on the same TJA position are dropped (only one note per position)
- A drum roll or spinner that runs into the next object ends one grid position before it
- One `.tja` holds Easy / Normal / Hard / Oni / Edit once each; more difficulties need a second run
- Difficulties are assumed to use the same audio; one `OFFSET` serves all of them (later starts get `#DELAY`)

Not converted at all: hit sound samples / volumes, storyboards, video, backgrounds, breaks and
osu!standard (Mode 0) beatmaps. `LEVEL`, branches, `SCOREINIT` / `SCOREDIFF` and balloon counts
that should differ from osu!'s are left for the TJA editor.

MP3 decoders differ in how they handle the encoder delay, so a chart can sound tens of
milliseconds off in a TJA player even when osu! is right. Check the timing and adjust with
`--offset` (or the `OFFSET` header) if needed.

## Project layout

```
tja2osz/
  cli.py          command line interface
  osz2tja.py      the reverse converter (osu!taiko -> TJA) and its command line
  convert.py      converts one .tja file and writes the .osz
  tja.py          TJA reading, validation and timing
  timeline.py     timeline events shared by the other modules
  osu.py          .osu writer
  lazer.py        port of the osu!lazer behaviour the output depends on
  verify.py       reads written files back and compares them with the TJA
  audio.py        converts and joins audio with ffmpeg
  diagnostics.py  errors and warnings
```

## License

[MIT](LICENSE)

## Acknowledgements

- [ppy/osu](https://github.com/ppy/osu) (MIT): `lazer.py` reimplements the osu!lazer logic for timing points, drum roll durations and bar lines
- [OpenTaiko](https://github.com/0auBSQ/OpenTaiko): reference for TJA parsing behaviour
