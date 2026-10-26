#!/usr/bin/env python3
"""Transcribe Windows Sound Recorder audio files into Obsidian markdown notes.

For every audio file in the recordings folder that does not yet have a
transcript note, runs Whisper (via faster-whisper) and writes one markdown
note per recording.  There is no state file: each run inspects the file
system.  A transcript note belongs to a recording via its frontmatter
`source` field, so notes are only skipped while they still exist — deleted
notes are simply recreated on the next run.

Configuration comes from environment variables (set in run.sh):
- TRANSCRIBER_SOURCE   folders to scan for audio files (':'-separated)
- TRANSCRIPTS_DIR      folder the transcript notes are written to
- TRANSCRIBER_MODEL    whisper model size (tiny, base, small, medium, large-v3)
- TRANSCRIBER_TZ       IANA time zone for timestamps
- TRANSCRIBER_LANGUAGE optional forced language code (de, en, ...)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

AUDIO_EXTENSIONS = {".m4a", ".wav", ".mp3", ".wma", ".flac", ".ogg", ".opus",
                    ".mka"}

# Segments are merged into one paragraph when the silence between them is
# shorter than this many seconds.
PARAGRAPH_GAP = 2.0

NOTE_PREFIX = "Transcript from "


def log(msg: str) -> None:
    if not QUIET:
        print(msg, flush=True)


def result(msg: str) -> None:
    """Final count line; always printed, even in quiet mode."""
    print(msg, flush=True)


QUIET = False


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def resolve(cli_value: str | None, env_name: str, required: bool = False) -> str | None:
    """CLI flag wins over environment variable; empty values count as unset."""
    value = (cli_value or os.environ.get(env_name) or "").strip() or None
    if required and not value:
        die(f"missing configuration: set {env_name} (usually done in run.sh)")
    return value


def recorder_name(source: Path) -> str:
    """Human-readable name of the app that produced a recording."""
    s = str(source)
    if "WindowsSoundRecorder" in s:
        return "Windows Sound Recorder"
    if "StreamTap" in s:
        return "StreamTap"
    return source.name


def format_duration(seconds: float) -> str:
    """Format a duration as H:MM:SS for use in file names and titles."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_clock(dt: datetime) -> str:
    """Format a datetime as YYYY-MM-DD HH:MM."""
    return dt.strftime("%Y-%m-%d %H:%M")


def sanitize_filename(name: str) -> str:
    """Remove characters Windows/Obsidian do not allow in file names."""
    return re.sub(r'[\\/:*?"<>|]', ".", name).strip()


# ---------------------------------------------------------------------------
# Metadata extraction (ffprobe)
# ---------------------------------------------------------------------------


@dataclass
class Meta:
    duration: float | None
    recorded_at: datetime | None  # timezone-aware, converted to target tz


def probe_metadata(path: Path, tz: ZoneInfo) -> Meta:
    """Read duration and creation_time from an audio file using ffprobe."""
    meta = Meta(duration=None, recorded_at=None)
    if shutil.which("ffprobe") is None:
        log("warning: ffprobe not found, using file timestamps only")
        return meta

    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"warning: ffprobe failed for {path.name}: {exc}")
        return meta

    data = json.loads(out or "{}")
    fmt = data.get("format", {})

    duration = fmt.get("duration")
    if duration is None:
        for stream in data.get("streams", []):
            if stream.get("duration"):
                duration = stream["duration"]
                break
    try:
        if duration is not None:
            meta.duration = float(duration)
    except (TypeError, ValueError):
        pass

    # creation_time may live in the container tags or in stream tags.
    creation = fmt.get("tags", {}).get("creation_time")
    if not creation:
        for stream in data.get("streams", []):
            creation = stream.get("tags", {}).get("creation_time")
            if creation:
                break
    if creation:
        meta.recorded_at = parse_creation_time(creation, tz)

    # Fall back to the file modification time if there is no embedded date.
    if meta.recorded_at is None:
        meta.recorded_at = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).astimezone(tz)
    return meta


def parse_creation_time(value: str, tz: ZoneInfo) -> datetime | None:
    """Parse ffprobe's ISO-8601 creation_time (usually UTC, '...Z')."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # ffprobe also writes shorter forms like '2024-01-02 15:36:25'
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(value, pattern)
                dt = dt.replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


# ---------------------------------------------------------------------------
# Transcript notes on disk
# ---------------------------------------------------------------------------


def note_source(path: Path) -> str:
    """The recording a transcript note belongs to (frontmatter `source`)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^source:\s*"?(.+?)"?\s*$', text, re.MULTILINE)
    return m.group(1) if m else ""


def existing_notes(dest: Path) -> dict[str, str]:
    """Map of transcript note file name -> recording it belongs to."""
    notes: dict[str, str] = {}
    if dest.is_dir():
        for note in dest.glob(f"{NOTE_PREFIX}*.md"):
            notes[note.name] = note_source(note)
    return notes


def free_variant(dest: Path, stem: str, taken: set[str]) -> Path:
    """First unused file name among stem.md, stem 2.md, stem 3.md, ..."""
    candidate = dest / f"{stem}.md"
    n = 1
    while candidate.name in taken:
        n += 1
        candidate = dest / f"{stem} {n}.md"
    taken.add(candidate.name)
    return candidate


# ---------------------------------------------------------------------------
# Transcription (faster-whisper)
# ---------------------------------------------------------------------------


def transcribe(path: Path, model_name: str, language: str | None,
               timestamps: bool) -> tuple[str, str]:
    """Transcribe an audio file; returns (markdown text, detected language)."""
    from faster_whisper import WhisperModel  # deferred: slow import

    model = WhisperModel(model_name, device="cpu", compute_type="int8")

    segments, info = model.transcribe(
        str(path), language=language, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    detected = info.language or "unknown"
    log(f"  detected language: {detected} "
        f"(probability {info.language_probability:.2f})")

    parts: list[str] = []
    buffer: list[str] = []
    last_end = 0.0

    def flush() -> None:
        if not buffer:
            return
        text = " ".join(part.strip() for part in buffer).strip()
        if text:
            parts.append(text)
        buffer.clear()

    for seg in segments:
        if buffer and seg.start - last_end > PARAGRAPH_GAP:
            flush()
        stamp = f"[{format_duration(seg.start)}] " if timestamps else ""
        buffer.append(f"{stamp}{seg.text}")
        last_end = seg.end
    flush()

    return "\n\n".join(parts).strip(), detected


# ---------------------------------------------------------------------------
# Note writing
# ---------------------------------------------------------------------------


def build_note(title: str, body: str, src: Path, recorder: str,
               recorded_str: str, transcribed_str: str, dur_str: str,
               model_name: str, language: str | None) -> str:
    day = recorded_str[:10] if recorded_str[:4].isdigit() else None
    lines = [
        "---",
        "type: recording/transcript",
        f"source: \"{src.name}\"",
        f"recorder: \"{recorder}\"",
        f"recorded: {recorded_str}",
        f"duration: \"{dur_str}\"",
        f"transcribed: {transcribed_str}",
        f"model: {model_name}",
        f"language: {language or 'auto'}",
        "tags: transcripts/full",
        "---",
        "",
        f"# {title}",
    ]
    if day:
        lines.append(f"Recorded on [[{day}]] at {recorded_str[11:]}.")
    lines.append("")
    if body:
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Transcribe Windows Sound Recorder recordings into "
                    "Obsidian markdown notes. Configuration comes from "
                    "environment variables (see module docstring).")
    p.add_argument("--model", default=None,
                   help="override TRANSCRIBER_MODEL")
    p.add_argument("--language", default=None,
                   help="override TRANSCRIBER_LANGUAGE (e.g. 'en', 'de')")
    p.add_argument("--tz", default=None,
                   help="override TRANSCRIBER_TZ (IANA time zone)")
    p.add_argument("--timestamps", action="store_true",
                   help="prefix transcript paragraphs with [H:MM:SS] stamps")
    p.add_argument("--force", action="store_true",
                   help="re-transcribe recordings that already have a note")
    p.add_argument("--dry-run", action="store_true",
                   help="only show what would be transcribed")
    p.add_argument("--quiet", action="store_true",
                   help="suppress progress output; only print the final count")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global QUIET
    QUIET = args.quiet

    source = resolve(None, "TRANSCRIBER_SOURCE", required=True)
    dest = Path(resolve(None, "TRANSCRIPTS_DIR", required=True))
    model_name = resolve(args.model, "TRANSCRIBER_MODEL", required=True)
    tz_name = resolve(args.tz, "TRANSCRIBER_TZ", required=True)
    language = resolve(args.language, "TRANSCRIBER_LANGUAGE")

    # TRANSCRIBER_SOURCE may contain several folders, separated by ':'.
    sources = [Path(p) for p in source.split(":") if p.strip()]
    if not sources:
        die("no recordings folders configured in TRANSCRIBER_SOURCE")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        die(f"unknown time zone: {tz_name}")

    recordings: list[tuple[Path, str]] = []
    for src_dir in sources:
        if not src_dir.is_dir():
            log(f"warning: recordings folder not found, skipping: {src_dir}")
            continue
        app = recorder_name(src_dir)
        for f in sorted(src_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                recordings.append((f, app))
    recordings.sort(key=lambda item: item[0])
    if not recordings:
        log("no audio files found in recordings folder(s)")
        result("transcripts: 0")
        return 0

    notes = existing_notes(dest)
    sources_done = set(notes.values())
    pending = [(f, app) for f, app in recordings
               if args.force or f.name not in sources_done]
    log(f"{len(pending)} of {len(recordings)} recording(s) to transcribe")
    if args.dry_run:
        for f, app in pending:
            log(f"  would transcribe: {f.name} ({app})")
        result(f"transcripts: {len(pending)} (dry run)")
        return 0

    if not pending:
        result("transcripts: 0")
        return 0

    try:
        import faster_whisper  # noqa: F401  (only check availability)
    except ImportError:
        die("faster-whisper is not installed. Run: uv sync")
    except ImportError:
        die("faster-whisper is not installed. Run: uv sync")

    taken = set(notes)
    transcribed = 0
    for f, app in pending:
        log(f"transcribing: {f.name} ({app})")
        meta = probe_metadata(f, tz)
        if meta.duration is None:
            log("  warning: no duration detected, using 0")

        transcribed_str = format_clock(datetime.now(tz))
        if meta.recorded_at is not None and meta.duration is not None:
            title = (f"Transcript from {format_clock(meta.recorded_at)}"
                     f" + {format_duration(meta.duration)}")
        else:
            # Fallback when the file carries no usable metadata.
            title = (f"Transcript transcribed on {transcribed_str}"
                     f" + {format_duration(meta.duration or 0)}")

        owned = sorted(name for name, src in notes.items() if src == f.name)
        if owned and args.force:
            dest_path = dest / owned[0]  # overwrite the existing own note
        else:
            dest_path = free_variant(dest, sanitize_filename(title), taken)

        body, detected = transcribe(f, model_name, language, args.timestamps)
        note = build_note(
            title=title,
            body=body,
            src=f,
            recorder=app,
            recorded_str=format_clock(meta.recorded_at) if meta.recorded_at else "unknown",
            transcribed_str=transcribed_str,
            dur_str=format_duration(meta.duration or 0),
            model_name=model_name,
            language=language or detected,
        )

        tmp = dest_path.with_suffix(".md.tmp")
        tmp.write_text(note, encoding="utf-8")
        tmp.replace(dest_path)

        notes[dest_path.name] = f.name
        transcribed += 1
        log(f"  wrote: {dest_path}")

    log(f"done: {transcribed} note(s) written")
    result(f"transcripts: {transcribed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
