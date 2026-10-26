#!/usr/bin/env python3
"""Generate Obsidian summaries for transcript notes using OpenCode AI models.

Scans the transcripts folder for "Transcript from ..." notes that do not
yet have a matching "Summary from ..." note and generates the summary by
sending the transcript to an AI model through the `opencode` CLI.  There is
no state: each run inspects the file system only.

Safety rule: existing files are never modified or deleted.  A summary is
only ever written when the target file does not exist yet; anything already
in the vault is treated as read-only.

Configuration comes from environment variables (set in run.sh):
- TRANSCRIPTS_DIR  folder with the transcript notes
- VAULT_DIR        Obsidian vault, whose page names are given as link context
- SUMMARY_MODEL    opencode model in provider/model form
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TRANSCRIPT_PREFIX = "Transcript from "
SUMMARY_PREFIX = "Summary from "
VAULT_EXCLUDE_DIRS = {".obsidian", ".trash", ".git", ".smart-env"}
MAX_VAULT_PAGES = 6000
OPENCODE_TIMEOUT = 900  # seconds per summary


def log(msg: str) -> None:
    if not QUIET:
        print(msg, flush=True)


def result(msg: str) -> None:
    """Final count line; always printed, even in quiet mode."""
    print(msg, flush=True)


def err(msg: str) -> None:
    """Errors are always printed, even in quiet mode."""
    print(msg, file=sys.stderr, flush=True)


QUIET = False


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        die(f"missing configuration: set {name} (usually done in run.sh)")
    return Path(value)


# ---------------------------------------------------------------------------
# Vault context
# ---------------------------------------------------------------------------


def list_vault_pages(vault: Path) -> list[str]:
    """Relative file names of all markdown pages in the vault."""
    pages: list[str] = []
    if not vault.is_dir():
        log(f"warning: vault folder not found: {vault}")
        return pages
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if any(part in VAULT_EXCLUDE_DIRS or part.startswith(".")
               for part in rel.parts[:-1]):
            continue
        pages.append(str(rel.with_suffix("")))
        if len(pages) >= MAX_VAULT_PAGES:
            log(f"warning: vault listing truncated at {MAX_VAULT_PAGES} pages")
            break
    return pages


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a note into (frontmatter dict, body). Very small YAML subset."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"')
    return meta, body


def recorded_date(transcript_path: Path) -> str | None:
    """The YYYY-MM-DD the recording was made, from frontmatter."""
    meta, _ = split_frontmatter(
        transcript_path.read_text(encoding="utf-8"))
    for key in ("recorded", "transcribed"):
        value = meta.get(key, "")
        m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Summary generation (opencode CLI)
# ---------------------------------------------------------------------------


def build_prompt(vault_pages: list[str], transcript_body: str) -> str:
    pages = "\n".join(f"- {p}" for p in vault_pages)
    return f"""You are turning a meeting transcript into an Obsidian markdown note.
Rules:
- Do not use any tools. Do not read, write or modify any files. Output text only.
- Write the summary in the same language as the transcript.
- Start your output directly with the "# ..." heading line. No preamble, no closing remarks, and do not wrap the note in a code fence.

Structure the note exactly like this:
1. `# <A descriptive title for the meeting>`
2. `## Participants` - a markdown table listing the potential participants:

   | Participant | Role / Notes |
   | --- | --- |

   IMPORTANT: every person's name must be an Obsidian wiki link in double
   square brackets: write [[Jochen Illerhaus, IRS]], NEVER plain text like
   Jochen Illerhaus, IRS. When a person likely has a page in the vault, use
   that page's exact name (see the vault page list below). Otherwise make up
   a well-formed link like [[Maria Muster, Institution]].
3. `## Summary` - a short overview, at most 3-5 sentences.
4. `## Details` - the most important points as concise bullet points, grouped by topic.

Wherever a person from the vault page list is mentioned anywhere in the note,
always refer to them as a wiki link [[...]], not as plain text.
Never omit any of the four sections or their headings, even if there is
nothing to report - keep the section with its heading and state briefly why
it is empty.

Style: be short, direct and to the point. No filler phrases, no hedging, no
repetition, no concluding pleasantries. Prefer tight bullet points over prose.
Do not make the Details section longer than necessary to capture the facts.

Pay particular emphasis on dates and deadlines: format every date you mention with the Obsidian link syntax [[YYYY-MM-DD]].
Only include a mermaid diagram (```mermaid code block) if it genuinely adds value, e.g. a timeline for a meeting with many dates or deadlines.

End your output with exactly one last line in this format:
LANGUAGE: <comma-separated ISO 639-1 codes of every language spoken in the meeting, e.g. "de" or "de, en">

Vault pages available for [[...]] links (names only):
{pages}

Transcript to summarize:
<<<TRANSCRIPT
{transcript_body}
TRANSCRIPT>>>
"""


def linkify_vault_pages(text: str, vault_pages: list[str]) -> str:
    """Turn plain-text mentions of vault pages into [[wiki links]].

    Belt-and-suspenders in case the model outputs a person as plain text.
    Only pages that look like person/institution pages (contain whitespace)
    are linked, to avoid over-linking; text inside code fences is skipped.
    """
    linkable = sorted((p for p in vault_pages if " " in p and len(p) > 3),
                      key=len, reverse=True)
    if not linkable:
        return text
    segments = text.split("```")
    for i in range(0, len(segments), 2):  # even indices are outside code fences
        for page in linkable:
            pattern = re.compile(r"(?<!\[\[)(?<!\|)\b" + re.escape(page) + r"\b")
            segments[i] = pattern.sub(f"[[{page}]]", segments[i])
    return "```".join(segments)


def extract_languages(output: str) -> tuple[list[str], str]:
    """Pull the trailing 'LANGUAGE: de, en' line out of the model output."""
    lines = output.rstrip().splitlines()
    if lines and lines[-1].upper().startswith("LANGUAGE:"):
        codes = [c.strip().lower() for c in
                 lines[-1].split(":", 1)[1].split(",") if c.strip()]
        return sorted(set(codes)), "\n".join(lines[:-1]).rstrip()
    return [], output


def clean_output(raw: str) -> str:
    """Strip ANSI codes, progress lines and code fences from opencode output."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    lines = text.splitlines()
    # Drop the 'run' banner lines like '> build · kit-local-for-opencode'.
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith(">")):
        lines.pop(0)
    text = "\n".join(lines).strip()
    # Remove one wrapping markdown fence, if the model added one.
    fence = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return text


def call_opencode(prompt: str, model: str) -> str:
    result = subprocess.run(
        ["opencode", "run", "--model", model, prompt],
        capture_output=True, text=True, timeout=OPENCODE_TIMEOUT,
        cwd="/tmp/opencode",  # agent has no business in the vault
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"opencode exited with {result.returncode}: {result.stderr.strip()}")
    return clean_output(result.stdout)


# ---------------------------------------------------------------------------
# Note writing
# ---------------------------------------------------------------------------


def build_summary_note(summary_body: str, transcript: Path,
                       day: str | None, languages: list[str],
                       model: str) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    up = f'"[[{day}]]"' if day else ""
    prev = f'"[[{transcript.stem}]]"'
    lang_line = f'languages: [{", ".join(languages)}]' if languages else None
    lines = [
        "---",
        "type: recording/summary",
        f'source: "{transcript.name}"',
        f"recorded: {day}" if day else "recorded: unknown",
        f"up: {up}" if up else None,
        f"prev: {prev}",
        lang_line,
        "tags: transcripts/summary",
        f"generated: {now}",
        f"model: {model}",
        "---",
        "",
    ]
    return "\n".join(line for line in lines if line is not None) + summary_body + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate AI summaries for transcript notes via opencode. "
                    "Configuration comes from environment variables "
                    "(see module docstring).")
    p.add_argument("--limit", type=int, default=None,
                   help="only summarize the first N pending transcripts")
    p.add_argument("--dry-run", action="store_true",
                   help="only show what would be summarized")
    p.add_argument("--quiet", action="store_true",
                   help="suppress progress output; only print the final count")
    return p.parse_args(argv)


def summarize_all(transcripts_dir: Path, vault: Path, model: str,
                  limit: int | None = None, dry_run: bool = False) -> int:
    if not transcripts_dir.is_dir():
        print(f"error: transcripts folder not found: {transcripts_dir}",
              file=sys.stderr)
        return 1

    transcripts = sorted(p for p in transcripts_dir.glob("*.md")
                         if p.name.startswith(TRANSCRIPT_PREFIX))
    pending: list[tuple[Path, Path]] = []
    for t in transcripts:
        summary = transcripts_dir / (SUMMARY_PREFIX + t.name[len(TRANSCRIPT_PREFIX):])
        # Existing summaries are treated as read-only and never regenerated.
        if summary.exists():
            continue
        pending.append((t, summary))

    log(f"{len(pending)} of {len(transcripts)} transcript(s) need a summary")
    if dry_run:
        for t, s in pending:
            log(f"  would summarize: {t.name} -> {s.name}")
        result(f"summaries: {len(pending)} (dry run)")
        return 0
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        result("summaries: 0")
        return 0

    vault_pages = list_vault_pages(vault)
    log(f"vault context: {len(vault_pages)} page names")

    done = 0
    for t, summary in pending:
        log(f"summarizing: {t.name}")
        meta, body = split_frontmatter(t.read_text(encoding="utf-8"))
        day = None
        m = re.match(r"(\d{4}-\d{2}-\d{2})", meta.get("recorded", ""))
        if m:
            day = m.group(1)
        try:
            output = call_opencode(build_prompt(vault_pages, body), model)
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            err(f"  error while summarizing {t.name}: {exc}")
            continue
        if not output:
            err(f"  error while summarizing {t.name}: empty response from model")
            continue
        languages, output = extract_languages(output)
        output = linkify_vault_pages(output, vault_pages)
        if not languages:
            # Fall back to the language detected during transcription.
            detected = meta.get("language", "")
            if detected and detected != "auto":
                languages = [detected]
        if summary.exists():  # re-check right before writing (paranoia)
            log(f"  skipped: {summary.name} appeared in the meantime")
            continue
        note = build_summary_note(output, t, day, languages, model)
        tmp = summary.with_suffix(".md.tmp")
        tmp.write_text(note, encoding="utf-8")
        tmp.replace(summary)
        done += 1
        log(f"  wrote: {summary.name}")
    log(f"done: {done} summary note(s) written")
    result(f"summaries: {done}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global QUIET
    QUIET = args.quiet
    if shutil.which("opencode") is None:
        die("opencode CLI not found on PATH "
            "(run.sh normally adds ~/.opencode/bin to PATH)")
    return summarize_all(env_path("TRANSCRIPTS_DIR"), env_path("VAULT_DIR"),
                         os.environ.get("SUMMARY_MODEL") or
                         die("missing configuration: set SUMMARY_MODEL "
                             "(usually done in run.sh)"),
                         limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
