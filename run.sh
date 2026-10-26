#!/bin/sh
# Single entry point: first transcribe new Windows Sound Recorder recordings,
# then generate AI summaries for all transcript notes that do not have one.
# The two steps are independent applications; both are stateless and only
# read existing files / write new ones.
#
# The folder and model configuration lives here (once) and is passed to the
# applications via environment variables.  Pre-setting a variable before
# calling run.sh overrides the default below.
#
# Output is kept minimal so the result can be shown in an Obsidian balloon
# (Shell Commands plugin): one line with the number of new transcripts and
# one with the number of new summaries.  Call with -v / --verbose for full
# progress output.
set -e
cd "$(dirname "$0")"

# Non-login WSL shells (e.g. `wsl -d Ubuntu -- ...` from cmd.exe) do not read
# .profile, so user-installed tools are not on PATH. Put them there explicitly.
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"

export TRANSCRIBER_SOURCE="${TRANSCRIBER_SOURCE:-/mnt/c/Users/dm5635/Documents/Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe!App/Audioaufzeichnungen:/mnt/c/Users/dm5635/Music/StreamTap}"
export TRANSCRIPTS_DIR="${TRANSCRIPTS_DIR:-/mnt/d/val/note/vault/transcriptions}"
export VAULT_DIR="${VAULT_DIR:-/mnt/d/val/note/vault}"
export TRANSCRIBER_MODEL="${TRANSCRIBER_MODEL:-small}"
export TRANSCRIBER_TZ="${TRANSCRIBER_TZ:-Europe/Berlin}"
export TRANSCRIBER_LANGUAGE="${TRANSCRIBER_LANGUAGE:-}"
export SUMMARY_MODEL="${SUMMARY_MODEL:-kit/kit-local-for-opencode}"

# Resolve uv explicitly: non-login WSL shells (e.g. `wsl -d Ubuntu -- ...`
# from cmd.exe) do not read .profile, so ~/.local/bin is not on PATH.
if ! UV="$(command -v uv)" || [ ! -x "$UV" ]; then
    UV="$HOME/.local/bin/uv"
fi
if [ ! -x "$UV" ]; then
    echo "error: uv not found (looked in PATH and $HOME/.local/bin/uv)" >&2
    exit 1
fi

if [ "$1" = "-v" ] || [ "$1" = "--verbose" ]; then
    shift
    "$UV" run transcriber.py "$@"
    "$UV" run summarizer.py
    exit 0
fi

# Quiet mode: capture everything; the scripts print a single count line plus
# any warnings/errors, which is exactly what a balloon notification needs.
out=$("$UV" run transcriber.py --quiet "$@" 2>&1) || status=$?
echo "$out"
if [ -n "$status" ] && [ "$status" -ne 0 ]; then
    exit "$status"
fi

out=$("$UV" run summarizer.py --quiet 2>&1) || status=$?
echo "$out"
if [ -n "$status" ] && [ "$status" -ne 0 ]; then
    exit "$status"
fi
