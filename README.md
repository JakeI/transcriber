# transcriber

Two small command-line applications (plus a single entry point) that turn
audio recordings into Obsidian notes:

- **`transcriber.py`** — transcribes recordings made with the standard
  Windows Sound Recorder app (and StreamTap recordings) using Whisper
  ([faster-whisper]) and writes one markdown transcript note per recording
  into the Obsidian vault.
- **`summarizer.py`** — generates AI meeting summaries for those transcript
  notes by sending them to an AI model through the `opencode` CLI
  (self-hosted KIT KI-Toolbox model by default).

`run.sh` sets all configuration once and calls the two applications one
after the other. It is the only command you normally need.

[faster-whisper]: https://github.com/SYSTRAN/faster-whisper

## Setup

Requirements (all present in this WSL environment):

- [uv](https://docs.astral.sh/uv/) for dependency management
- `ffmpeg`/`ffprobe` on the `PATH`
- the `opencode` CLI (with the KIT KI-Toolbox provider configured)

Install dependencies:

```sh
uv sync
```

The first transcriber run downloads the selected Whisper model into
`~/.cache/huggingface`.

## Usage

```sh
./run.sh                    # transcribe new recordings + generate missing summaries
./run.sh --dry-run          # see what would be transcribed
./run.sh -v                 # full verbose progress output
TRANSCRIBER_MODEL=large-v3 ./run.sh   # higher transcription quality
```

By default the output is minimal — one `transcripts: N` and one
`summaries: N` line — so it can be shown directly in an Obsidian balloon
notification (e.g. via the *Shell Commands* plugin). Warnings and errors are
still printed when they occur. Use `run.sh -v` for detailed progress; extra
arguments (like `--dry-run`) are forwarded to the transcriber.

Both applications are **stateless**: every run inspects the file system only.

- A transcript note is linked to its recording via the `source` field in its
  frontmatter, so recordings are skipped exactly as long as their note
  exists — deleting a note makes the next run recreate it.
- A summary is generated for every transcript note that has no matching
  summary note yet; if a run is interrupted, the next run simply catches up.
- Existing files are never modified or deleted (details under
  [Read-only vault guarantee](#read-only-vault-guarantee)).

## Configuration

All configuration lives in `run.sh` (each value can be overridden by
pre-setting the variable before calling `run.sh`):

| Variable               | Purpose                                                        |
| ---------------------- | -------------------------------------------------------------- |
| `TRANSCRIBER_SOURCE`   | folders to scan for audio recordings (`:`-separated)           |
| `TRANSCRIPTS_DIR`      | folder for transcript/summary notes (used by both apps)        |
| `VAULT_DIR`            | Obsidian vault, source of page names for AI linking            |
| `TRANSCRIBER_MODEL`    | whisper model size (`tiny`, `base`, `small`, `medium`, `large-v3`) |
| `TRANSCRIBER_TZ`       | IANA time zone for timestamps (default `Europe/Berlin`)        |
| `TRANSCRIBER_LANGUAGE` | optional forced language code (empty = auto-detect)            |
| `SUMMARY_MODEL`        | opencode model for summaries (`provider/model`)                |

Note: `run.sh` also adds `~/.opencode/bin` and `~/.local/bin` to `PATH`
itself, so it works even from non-login shells such as
`wsl -d Ubuntu -- /mnt/d/val/note/transcriber/run.sh` launched from cmd.exe,
Task Scheduler, cron or the Obsidian Shell Commands plugin.

### Transcriber options

| Option         | Description                                          |
| -------------- | ---------------------------------------------------- |
| `--model`      | override `TRANSCRIBER_MODEL`                         |
| `--language`   | override `TRANSCRIBER_LANGUAGE`                      |
| `--tz`         | override `TRANSCRIBER_TZ`                            |
| `--timestamps` | Prefix paragraphs with `[H:MM:SS]` position stamps   |
| `--force`      | Re-transcribe recordings that already have a note    |
| `--dry-run`    | Only list what would be transcribed                  |
| `--quiet`      | Only print the final count line (used by run.sh)     |

`small` is a good CPU trade-off; use `large-v3` for maximum quality on a
longer session. If a short clip is mis-detected (e.g. German heard as
English), force it with `TRANSCRIBER_LANGUAGE=de` or `--language de`.

### Summarizer options

| Option      | Description                                       |
| ----------- | ------------------------------------------------- |
| `--limit N` | Only summarize the first N pending transcripts    |
| `--dry-run` | Only list what would be summarized                |
| `--quiet`   | Only print the final count line (used by run.sh)  |

## Transcript notes

For every audio file in the source folders that does not yet have a
transcript note, the transcriber writes
`Transcript from YYYY-MM-DD HH:mm + H:MM:SS.md`, where the timestamp comes
from the recording's embedded metadata (`creation_time`, converted to local
time) and the duration from the file itself. If a file has no usable
metadata, the note is titled `Transcript transcribed on YYYY-MM-DD HH:mm +
H:MM:SS` using the time of transcription. Because `:` is not allowed in file
names, it is replaced with `.` in file names but kept in the note title.
Two recordings with identical titles get numbered suffixes (`... 2.md`).

Note structure:

```markdown
---
type: recording/transcript
source: "StreamTap_20261026_204811.mp3"
recorder: "StreamTap"
recorded: 2026-10-26 20:48
duration: "00:00:55"
transcribed: 2026-10-26 20:51
model: small
language: en
tags: transcripts/full
---

# Transcript from 2026-10-26 20:48 + 00:00:55
Recorded on [[2026-10-26]] at 20:48.

<transcript text>
```

- `source` links the note back to the recording file (this is how the
  stateless skip logic works).
- `recorder` names the app the file came from (e.g. Windows Sound Recorder,
  StreamTap) based on which source folder it was found in.
- `Recorded on [[YYYY-MM-DD]]` under the heading links to the daily note.
- Pauses longer than 2 seconds start a new paragraph.
- Tag: `transcripts/full`.

## Summary notes

`summarizer.py` scans the transcripts folder for `Transcript from ...` notes
that have no matching `Summary from ...` note and generates one for each.
The transcript — together with the list of all vault page names as context —
is sent to the AI model configured in `SUMMARY_MODEL` (by default
`kit/kit-local-for-opencode` from the KIT KI-Toolbox).

The prompt asks the model to produce:

1. a descriptive title,
2. a `## Participants` markdown table, linking people with the vault's
   `[[First Last, Institution]]` convention (e.g. `[[Jochen Illerhaus, IRS]]`)
   when a matching vault page exists,
3. a short `## Summary` (at most 3-5 sentences),
4. concise `## Details` bullet points, grouped by topic.

Dates and deadlines must be formatted as `[[YYYY-MM-DD]]`; mermaid diagrams
are allowed when they genuinely help; the model reports the meeting
language(s). As a safety net, plain-text mentions of vault person pages are
converted into `[[wiki links]]` automatically after generation (code blocks
and existing links are left alone).

The result is written as `Summary from YYYY-MM-DD HH:mm + H:MM:SS.md` with:

```markdown
---
type: recording/summary
source: "Transcript from 2026-10-26 20.48 + 00.00.55.md"
recorded: 2026-10-26
up: "[[2026-10-26]]"
prev: "[[Transcript from 2026-10-26 20.48 + 00.00.55]]"
languages: [en]
tags: transcripts/summary
generated: 2026-10-26 20:51
model: kit/kit-local-for-opencode
---
```

- `up` links to the daily note of the recording.
- `prev` links back to the transcript note (clickable in Obsidian).
- `languages` lists the languages spoken in the meeting (reported by the
  model, with the transcriber's detected language as fallback).

## Read-only vault guarantee

Existing files are never modified or deleted by either application:

- Transcripts are only written for recordings that have no note yet (with
  `--force`, an existing note owned by that recording is overwritten —
  nothing else is touched).
- Summaries are only written when the target file does not exist yet; the
  existence is re-checked immediately before writing.

Manual edits to any note are therefore safe between runs.

## Triggering it / keeping Obsidian in sync

Obsidian watches the vault folder on disk and picks up new/changed files on
its own (worst case after re-focusing the window or `Ctrl+P` → "Reload app
without saving"), so no extra refresh step is needed. Ways to trigger
`run.sh`:

- **From inside Obsidian** — the *Shell Commands* community plugin can bind
  a command or ribbon icon to
  `wsl.exe -d Ubuntu -- /mnt/d/val/note/transcriber/run.sh` and show the
  `transcripts: N / summaries: N` output as a balloon notification.
- **Windows Task Scheduler** — a task running
  `wsl.exe -d Ubuntu -- /mnt/d/val/note/transcriber/run.sh` after logon or
  every 30 minutes keeps everything current automatically.
- **WSL cron / systemd timer** — equivalent cron entry:
  `*/30 * * * * /mnt/d/val/note/transcriber/run.sh >> /tmp/opencode/transcriber.log 2>&1`
- **Manually** — `./run.sh` from a terminal.

## Microsoft Teams recordings

Teams has built-in meeting recording and transcription, but it is
policy-controlled and quite different from the Sound Recorder workflow
(details from Microsoft Learn, [recording
policies](https://learn.microsoft.com/en-us/microsoftteams/teams-recording-policy),
[storage](https://learn.microsoft.com/en-us/microsoftteams/tmr-meeting-recording-change)):

- **Native convenience recording**: started ad-hoc by a licensed user, needs
  `allowCloudRecording` (and for transcripts, transcription) enabled in the
  tenant's meeting policy — i.e. admin approval. Recordings are saved as
  `.mp4` to the **organizer's OneDrive "Recordings" folder** (channel
  meetings: the team's SharePoint `Documents/Recordings` folder); the
  built-in transcript is stored alongside it. Guests cannot start
  recordings, and all participants are notified. About 400 MB per hour.
- **Compliance recording**: third-party bots that record everything
  automatically; company-owned, no per-user control. Overkill here.

Practical options for this workflow, roughly from least to most intrusive:

1. **Keep it local.** Record the call like with Sound Recorder (loopback
   capture of the meeting audio, e.g. StreamTap); the file lands in a local
   folder this pipeline already watches. No Teams policy involved.
2. **Teams native recording + OneDrive sync.** If your tenant allows
   recording, the `.mp4` files land in OneDrive *Recordings*; sync OneDrive
   to Windows and add the synced folder to `TRANSCRIBER_SOURCE`. Bonus:
   Teams' own transcript (`.vtt`/`.docx`) is downloadable next to the
   recording, though this pipeline transcribes the audio itself anyway.
   Restrictions: needs tenant policy + participant notification, no
   whiteboard/notes capture.
3. **Teams built-in transcription only.** If recording is disallowed but
   transcription is on, the live transcript can be downloaded afterwards
   (.docx/.vtt) and converted to a transcript note — no Whisper needed, but
   no local copy of the audio either.

## Project layout

| File             | Purpose                                              |
| ---------------- | ---------------------------------------------------- |
| `run.sh`         | single entry point; all configuration lives here     |
| `transcriber.py` | recordings → transcript notes (Whisper)              |
| `summarizer.py`  | transcript notes → AI summaries (opencode)           |
| `pyproject.toml` | uv project definition (dependencies)                 |
| `uv.lock`        | locked dependency versions                           |
