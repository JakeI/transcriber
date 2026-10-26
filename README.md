# transcriber

A small command-line tool that transcribes the recordings made with the
standard Windows Sound Recorder app and files them into an Obsidian vault.

For every audio file in the Windows Sound Recorder folder that has not been
transcribed yet, it runs Whisper (via [faster-whisper]) and writes one
markdown note per recording into `D:\val\note\vault\transcriptions`.

[faster-whisper]: https://github.com/SYSTRAN/faster-whisper

## Setup

Requires [uv](https://docs.astral.sh/uv/), `ffmpeg`/`ffprobe` on the `PATH`
(already installed in this WSL environment) and the `opencode` CLI for the
summaries. Dependencies are managed by uv from `pyproject.toml`:

```sh
uv sync
```

The first run downloads the selected Whisper model into `~/.cache/huggingface`.

## Usage

`run.sh` is the single entry point. It sets all configuration once (folders,
models, time zone) as environment variables and then calls the two
independent command-line applications one after the other:

```sh
./run.sh                    # transcribe new recordings + generate missing summaries
./run.sh --dry-run          # see what would be transcribed
./run.sh -v                 # full verbose progress output
TRANSCRIBER_MODEL=large-v3 ./run.sh   # higher transcription quality
```

By default the output is minimal — one `transcripts: N` and one
`summaries: N` line — so it can be shown directly in an Obsidian balloon
notification (e.g. via the *Shell Commands* plugin). Warnings and errors are
still printed when they occur. Use `run.sh -v` for detailed progress.

### Applications

The two applications are fully independent; `run.sh` only calls them one
after the other (extra command-line arguments go to the transcriber).

| Application     | Purpose                                                        |
| --------------- | -------------------------------------------------------------- |
| `transcriber.py` | transcribe new recordings into transcript notes               |
| `summarizer.py`  | generate AI summaries for transcript notes without one        |

Both are stateless: each run inspects the file system only. A transcript
note is linked to its recording via the `source` field in its frontmatter,
so recordings are skipped exactly as long as their note still exists — a
deleted note is simply recreated on the next run.

### Configuration (environment variables, set in run.sh)

| Variable               | Purpose                                              |
| ---------------------- | ---------------------------------------------------- |
| `TRANSCRIBER_SOURCE`   | folders to scan for audio recordings (`:`-separated) |
| `TRANSCRIPTS_DIR`      | folder for transcript/summary notes (shared)         |
| `VAULT_DIR`            | Obsidian vault, source of page names for AI linking  |
| `TRANSCRIBER_MODEL`    | whisper model size (`tiny` … `large-v3`)             |
| `TRANSCRIBER_TZ`       | IANA time zone for timestamps                        |
| `TRANSCRIBER_LANGUAGE` | optional forced language code (empty = auto-detect)  |
| `SUMMARY_MODEL`        | opencode model for summaries (`provider/model`)      |

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

### When to run it / keeping Obsidian in sync

Obsidian watches the vault folder on disk and picks up new/changed files on
its own (worst case after re-focusing the window or `Ctrl+P` → "Reload app
without saving"), so no extra refresh step is needed. Ways to trigger
`run.sh`:- **Manually** — just run `./run.sh` from a terminal when recordings exist.
- **From inside Obsidian** — the *Shell Commands* community plugin can bind
  a command (or ribbon icon) to `/mnt/d/val/note/transcriber/run.sh`, giving
  a one-click trigger that also keeps the vault refresh immediate.
- **Windows Task Scheduler** — a task running
  `wsl.exe -e /mnt/d/val/note/transcriber/run.sh` after logon/every 30 min
  keeps everything current without any manual action.
- **WSL cron / systemd timer** — equivalent cron entry:
  `*/30 * * * * /mnt/d/val/note/transcriber/run.sh >> /tmp/opencode/transcriber.log 2>&1`

### Options

| Option          | Default                          | Description                                          |
| --------------- | -------------------------------- | ---------------------------------------------------- |
| `--source DIR`  | Windows Sound Recorder folder    | Folder to scan for audio files                       |
| `--dest DIR`    | `vault/transcriptions`           | Folder the markdown notes are written to             |
| `--model SIZE`  | `small`                          | `tiny`, `base`, `small`, `medium`, `large-v3` …      |
| `--language DE` | auto-detect                      | Force a language, e.g. `de` or `en`                  |
| `--tz ZONE`     | `Europe/Berlin`                  | Time zone for the recording timestamps               |
| `--timestamps`  | off                              | Prefix paragraphs with `[H:MM:SS]` position stamps   |
| `--force`       | off                              | Re-transcribe files that were already transcribed    |
| `--no-summarize` | off                             | Skip the AI summary step                             |
| `--summary-model M` | `kit/kit-local-for-opencode` | OpenCode model used for the summaries                |
| `--dry-run`     | off                              | Only list what would be transcribed                  |

`small` is a good CPU trade-off; use `large-v3` for maximum quality on a
longer session. If a short clip is mis-detected (e.g. German heard as
English), force it with `--language de`.

## Notes

Filenames are `Transcript from YYYY-MM-DD HH:mm + H:MM:SS.md`, where the
timestamp comes from the recording's embedded `creation_time` metadata
(converted to local time) and the duration comes from the file itself.
If a file has no usable metadata, the note is titled
`Transcript transcribed on YYYY-MM-DD HH:mm + H:MM:SS` using the time of
transcription. Because `:` is not allowed in file names, it is replaced
with `.` in file names but kept in the note title.

Each note has YAML frontmatter (`type: recording/transcript`, source file,
the app that produced it (`recorder`, e.g. Windows Sound Recorder or
StreamTap), recorded/duration/transcribed times, model, language, tag
`transcripts/full`) followed by the transcript. A `Recorded on
[[YYYY-MM-DD]]` line under the heading links to the daily note. Pauses
longer than 2 seconds start a new paragraph.

## Summaries

`summarizer.py` scans the transcripts folder for `Transcript from ...`
notes that have no matching `Summary from ...` note and generates one for
each by sending the transcript to an AI model through the `opencode` CLI
(model from `SUMMARY_MODEL`, by default `kit/kit-local-for-opencode` from
the KIT KI-Toolbox):

```sh
uv run summarizer.py                 # summarize all pending notes
uv run summarizer.py --dry-run       # list pending notes
uv run summarizer.py --limit 3       # only the first three
```

The prompt tells the model to produce a title, a participants markdown
table, a short summary and a concise detailed account, with special emphasis
on dates (formatted as `[[YYYY-MM-DD]]`) and people. To let the model link
people with the vault's `[[First Last, Institution]]` convention, the list
of all vault page names is embedded in the prompt as context. As a safety
net, plain-text mentions of vault person pages in the model's output are
converted into `[[wiki links]]` automatically (text inside code blocks and
existing links are left alone). Mermaid diagrams are allowed.

Summary notes get frontmatter with `type: recording/summary`, the meeting
languages (`languages: [de]`, detected by the model with the transcript's
detected language as fallback), tag `transcripts/summary`, an
`up: "[[YYYY-MM-DD]]"` property pointing to the daily note of the recording
and a `prev: "[[Transcript from ...]]"` property linking back to the
transcript note.

**The vault is read-only for this tool.** Existing files are never modified
or deleted; a summary is only written when the target file does not exist
yet. Manual edits to summaries are therefore safe between runs.

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
   capture of the meeting audio); the file lands in a local folder this
   pipeline can watch directly (set `TRANSCRIBER_SOURCE` accordingly). No
   Teams policy involved.
2. **Teams native recording + OneDrive sync.** If your tenant allows
   recording, the `.mp4` files land in OneDrive *Recordings*; sync OneDrive
   to Windows (`C:\Users\dm5635\OneDrive - KIT\Recordings`) and point
   `TRANSCRIBER_SOURCE` at the synced folder. Bonus: Teams' own transcript
   (`.vtt`/`.docx`) is downloadable next to the recording, though this
   pipeline transcribes the audio itself anyway. Restrictions: needs tenant
   policy + participant notification, no whiteboard/notes capture.
3. **Teams built-in transcription only.** If recording is disallowed but
   transcription is on, the live transcript can be downloaded afterwards
   (.docx/.vtt) and converted to a transcript note — no Whisper needed, but
   no local copy of the audio either.
