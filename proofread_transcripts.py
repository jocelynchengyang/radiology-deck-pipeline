#!/usr/bin/env python3
"""
Proofread ASR-generated lecture transcripts with Claude, fixing only clear
mechanical errors (misheard words, technical terms, punctuation, capitalization,
sentence boundaries, stutters) without paraphrasing or changing meaning.

By default it processes every .txt in the `ultrasound-physics-transcripts/`
folder next to this script, editing each file in place and printing a unified
diff of the changes.

Source of truth is the original: the first time a file is touched, the original
is copied to `<file>.txt.orig`; on later runs the proofread starts from that
`.orig` again, so the script is safe to re-run and resume.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-api03-...      # or: ANTHROPIC_API_KEY=$(anthropic_key)
    pip install anthropic

    python proofread_transcripts.py                 # proofread the default folder
    python proofread_transcripts.py --dry-run       # show diffs, write nothing
    python proofread_transcripts.py PATH ...        # specific files or a folder
    python proofread_transcripts.py --limit 3       # only the first 3 files
    python proofread_transcripts.py --force         # redo files even if unchanged since last run
    python proofread_transcripts.py --model claude-sonnet-5

Requires: anthropic (pip install anthropic), Python 3.9+
"""

from __future__ import annotations

import argparse
import difflib
import sys
import time
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent / "ultrasound-physics-transcripts"
DEFAULT_MODEL = "claude-opus-5"

# Split long transcripts on sentence/line boundaries so each request stays small
# and one dropped paragraph can't wreck a whole file.
CHUNK_CHAR_TARGET = 9_000

SYSTEM_PROMPT = """\
You are a meticulous transcript proofreader. You receive the raw text of an \
automatically generated (speech-to-text) transcript of an ultrasound / radiology \
physics lecture. It may arrive with no punctuation and no line breaks.

Fix ONLY clear, objective errors:
- Misrecognized words and homophones where context makes the intended word \
unambiguous (there/their/they're, to/too, its/it's, principle/principal, of/off).
- Misheard domain terms from ultrasound and radiology physics, e.g. \
"ratification" -> "rarefaction", "pizza electric" / "piezo electric" -> \
"piezoelectric", "dampening block" -> "damping block", "an echoic" -> "anechoic", \
"hyper echoic" -> "hyperechoic", "an isotropic" -> "anisotropic", "Nyquist", \
"aliasing", "spectral broadening", "attenuation".
- Punctuation, capitalization, sentence boundaries, and stray spacing. Add \
sentence-ending punctuation and capitalization where the ASR omitted it.
- Clearly garbled numbers and units, e.g. "one 540 meters per second" -> \
"1540 meters per second", "three db" -> "3 dB", "mega hertz" -> "megahertz".
- Verbatim stutters and immediate accidental repetitions ("the the", "we we").

Do NOT:
- Paraphrase, reword, condense, expand, or "improve" any phrasing.
- Add or remove sentences, facts, examples, numbers, or explanations.
- Change the speaker's wording, register, or normal filler ("now", "so", \
"basically", "let's have a look") beyond true stutters.
- Alter meaning in any way.

Formatting of your output: put roughly one sentence per line (a newline after \
each sentence-ending period). If you cannot determine an intended word with \
confidence, leave it unchanged.

Output ONLY the corrected transcript text: no preamble, no commentary, no \
explanations, no markdown code fences.
"""

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def hard_wrap_long_line(line: str, limit: int) -> list[str]:
    """Break a very long unpunctuated 'line' on nearby spaces so no chunk is huge."""
    if len(line) <= limit:
        return [line]
    out, start = [], 0
    while start < len(line):
        end = min(start + limit, len(line))
        if end < len(line):
            sp = line.rfind(" ", start, end)
            if sp > start:
                end = sp
        out.append(line[start:end])
        start = end + 1 if end < len(line) and line[end] == " " else end
    return out


def chunk_text(text: str, target: int = CHUNK_CHAR_TARGET) -> list[str]:
    pieces: list[str] = []
    for raw in text.splitlines(keepends=True):
        pieces.extend(hard_wrap_long_line(raw, target * 2))
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for piece in pieces:
        buf.append(piece)
        size += len(piece)
        if size >= target:
            chunks.append("".join(buf))
            buf, size = [], 0
    if buf:
        chunks.append("".join(buf))
    return chunks or [text]


def strip_wrapper(out: str) -> str:
    """Defensively remove a stray ``` fence or 'Here is...' preamble."""
    s = out.strip("\n")
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip("\n")


def looks_sane(original: str, corrected: str) -> bool:
    o, c = len(original.split()), len(corrected.split())
    if o == 0:
        return c == 0
    return 0.8 <= c / o <= 1.25


def create_with_retry(client, anthropic_mod, **kwargs):
    delay = 4.0
    last = None
    for attempt in range(6):
        try:
            return client.messages.create(**kwargs)
        except anthropic_mod.APIStatusError as e:
            if e.status_code not in RETRYABLE_STATUS:
                raise
            last = e
        except (anthropic_mod.APIConnectionError, anthropic_mod.APITimeoutError) as e:
            last = e
        print(f"    . retrying after {delay:.0f}s ({type(last).__name__})", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 90)
    raise last


def proofread_chunk(client, anthropic_mod, model: str, chunk: str) -> str:
    for attempt in range(2):
        resp = create_with_retry(
            client, anthropic_mod,
            model=model,
            max_tokens=16_000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": chunk}],
        )
        text = strip_wrapper("".join(b.text for b in resp.content if b.type == "text"))
        if looks_sane(chunk, text):
            return text
        print("    ! implausible result, retrying chunk", file=sys.stderr)
    print("    ! keeping original for this chunk (could not verify)", file=sys.stderr)
    return chunk.strip("\n")


def proofread_text(client, anthropic_mod, model: str, original: str) -> str:
    parts = [
        proofread_chunk(client, anthropic_mod, model, ch)
        for ch in chunk_text(original)
    ]
    return "\n".join(p.strip("\n") for p in parts).strip("\n") + "\n"


def unified_diff(a: str, b: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


def collect_targets(args_paths: list[str]) -> list[Path]:
    roots = [Path(p).expanduser() for p in args_paths] if args_paths else [DEFAULT_DIR]
    files: list[Path] = []
    for r in roots:
        if r.is_dir():
            files.extend(sorted(p for p in r.glob("*.txt") if not p.name.endswith(".orig")))
        elif r.is_file():
            files.append(r)
        else:
            print(f"skip (not found): {r}", file=sys.stderr)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("paths", nargs="*", help="files or folders (default: ./ultrasound-physics-transcripts)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="print diffs, do not write")
    ap.add_argument("--limit", type=int, default=0, help="process at most N files")
    ap.add_argument("--force", action="store_true", help="reprocess even if .txt already matches a prior proofread")
    args = ap.parse_args()

    try:
        import anthropic
    except ImportError:
        print("Missing dependency. Run:  pip install anthropic", file=sys.stderr)
        return 2

    targets = collect_targets(args.paths)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("No .txt files to process.", file=sys.stderr)
        return 1

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ant profile
    changed = skipped = failed = 0

    for i, path in enumerate(targets, 1):
        backup = path.with_name(path.name + ".orig")
        source = backup if backup.exists() else path
        original = source.read_text(encoding="utf-8")
        current = path.read_text(encoding="utf-8")

        # Resume: if we already produced this .txt from this .orig, skip it.
        if backup.exists() and not args.force and current != original:
            print(f"[{i}/{len(targets)}] {path.name}  (already done, skipping)")
            skipped += 1
            continue

        print(f"[{i}/{len(targets)}] {path.name}")
        try:
            corrected = proofread_text(client, anthropic, args.model, original)
        except anthropic.APIError as e:
            print(f"    ! API error, left unchanged: {e}", file=sys.stderr)
            failed += 1
            continue

        if corrected == original:
            print("    no changes")
            continue

        changed += 1
        print(unified_diff(original, corrected, path.name) or "    (whitespace-only changes)")
        if args.dry_run:
            continue

        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
        path.write_text(corrected, encoding="utf-8")

    verb = "would change" if args.dry_run else "changed"
    tail = f", {failed} failed" if failed else ""
    print(f"\nDone. {verb} {changed}/{len(targets)} file(s); {skipped} already done{tail}.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
