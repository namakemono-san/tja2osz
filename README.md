# tja2osz

Convert TJA (Taiko Jiro / TJAPlayer / OpenTaiko) charts into osu!taiko beatmaps (`.osz`).

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

## Project layout

```
tja2osz/
  cli.py          command line interface
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
