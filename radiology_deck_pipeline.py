#!/usr/bin/env python3
"""
radiology_deck_pipeline.py
=========================

End-to-end pipeline that turns a YouTube lecture playlist into an Anki `.apkg`
of cloze cards, following the RadiologyTutorials / AnKore card-writing principles.

    playlist URL
        -> download auto-captions (yt-dlp)           [download]
        -> clean to one-sentence-per-line .txt        [download]
        -> (optional) proofread with Claude           [proofread]
        -> generate ~10-15 high-yield cloze cards      [cards]     (Claude)
           per lecture -> cards.json
        -> build <Deck>.apkg (genanki)                 [build]

Requirements
------------
    pip install yt-dlp anthropic genanki
    export ANTHROPIC_API_KEY=sk-ant-api03-...        (or `ANTHROPIC_API_KEY=$(anthropic_key)`)

Commands
--------
    run       full pipeline for one playlist
    download  playlist URL  -> transcripts folder (.txt + .txt.orig)
    proofread transcripts folder, in place (resumable; needs API)
    cards     transcripts folder -> cards.json (needs API)
    build     cards.json -> <Deck>.apkg
    batch     run every deck in a --config JSON file

Examples
--------
    python radiology_deck_pipeline.py run \\
        "https://www.youtube.com/playlist?list=PLxxxx" ./ultrasound \\
        --deck Ultrasound --tag-ns Ultrasound \\
        --section-prefix "Ultrasound Physics" \\
        --source "Ultrasound Physics Course #{num}" \\
        --proofread

    python radiology_deck_pipeline.py run "URL" ./ct \\
        --deck CT --tag-ns CT --section-prefix "CT Physics" \\
        --source "CT Physics Course Lesson #{num}" --reverse

    python radiology_deck_pipeline.py batch --config decks.json

decks.json (batch):
    [
      {"playlist":"URL","workdir":"./ultrasound","deck":"Ultrasound",
       "tag_ns":"Ultrasound","section_prefix":"Ultrasound Physics",
       "source":"Ultrasound Physics Course #{num}","proofread":true},
      {"playlist":"URL","workdir":"./ct","deck":"CT","tag_ns":"CT",
       "section_prefix":"CT Physics","source":"CT Physics Course Lesson #{num}",
       "reverse":true}
    ]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5"
CHUNK_CHARS = 9_000
RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}

# --------------------------------------------------------------------------- #
#  API key / client
# --------------------------------------------------------------------------- #

def get_api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    # macOS keychain helper (matches ~/.zshrc: security -s anthropic_api_key)
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""),
             "-s", "anthropic_api_key", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip().startswith("sk-ant"):
            return out.stdout.strip()
    except Exception:
        pass
    return None


def make_client():
    import anthropic
    key = get_api_key()
    if not key:
        sys.exit("No ANTHROPIC_API_KEY found (env or macOS keychain 'anthropic_api_key').")
    return anthropic.Anthropic(api_key=key), anthropic


def create_with_retry(client, anthropic_mod, **kw):
    delay = 4.0
    last = None
    for _ in range(6):
        try:
            return client.messages.create(**kw)
        except anthropic_mod.APIStatusError as e:
            if e.status_code not in RETRYABLE:
                raise
            last = e
        except (anthropic_mod.APIConnectionError, anthropic_mod.APITimeoutError) as e:
            last = e
        print(f"    . retry in {delay:.0f}s ({type(last).__name__})", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 90)
    raise last

# --------------------------------------------------------------------------- #
#  yt-dlp download + transcript cleaning
# --------------------------------------------------------------------------- #

def _ytdlp_cmd() -> list[str]:
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


def list_playlist(url: str) -> list[tuple[int, str, str]]:
    out = subprocess.run(
        _ytdlp_cmd() + ["--flat-playlist", "--print", "%(playlist_index)s\t%(id)s\t%(title)s", url],
        capture_output=True, text=True, check=True,
    )
    rows = []
    for line in out.stdout.splitlines():
        if line.startswith("[") or "\t" not in line:
            continue
        idx, vid, title = line.split("\t", 2)
        rows.append((int(idx), vid, title))
    return rows


def download(url: str, workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"[download] {url} -> {workdir}")
    # cache playlist index -> (video id, raw title) for later steps
    (workdir / ".playlist.json").write_text(
        json.dumps({str(i): [v, t] for i, v, t in list_playlist(url)}),
        encoding="utf-8")
    subprocess.run(
        _ytdlp_cmd() + [
            "--skip-download", "--write-auto-subs", "--sub-langs", "en-orig,en",
            "--sub-format", "json3",
            "-o", str(workdir / "%(playlist_index)02d - %(title)s.%(ext)s"),
            url,
        ],
        check=True,
    )
    _json3_to_text(workdir)
    for f in workdir.glob("*.json3"):
        f.unlink()
    print(f"[download] {len(list(workdir.glob('*.txt')))} transcripts written")


_ABBR = re.compile(r"\b(Dr|Mr|Mrs|Ms|Prof|St|vs|etc|e\.g|i\.e)\.", re.I)
_PH = ""  # private-use sentinel for a protected abbreviation period


def _json3_to_one_text(f: Path) -> str:
    data = json.loads(f.read_text(encoding="utf-8"))
    parts = [seg.get("utf8", "")
             for ev in data.get("events", [])
             for seg in (ev.get("segs") or [])]
    text = "".join(parts).replace("\n", " ")
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _ABBR.sub(lambda m: m.group(0)[:-1] + _PH, text)
    text = re.sub(r"(?<![A-Z])(?<=[.!?])\s+(?=[A-Z0-9])", "\n", text)
    return text.replace(_PH, ".") + "\n"


def _json3_to_text(workdir: Path) -> None:
    # one .txt per lecture stem, preferring the en-orig caption track
    stems: dict[str, Path] = {}
    for f in workdir.glob("*.json3"):
        if f.name.endswith(".en-orig.json3"):
            stems[f.name[: -len(".en-orig.json3")]] = f
        elif f.name.endswith(".en.json3"):
            stems.setdefault(f.name[: -len(".en.json3")], f)
    for stem, f in sorted(stems.items()):
        (workdir / f"{stem}.txt").write_text(_json3_to_one_text(f), encoding="utf-8")


def clean_title(stem: str) -> str:
    """'05 - Acoustic Impedance ｜ Ultrasound Physics ｜ ...'  ->  'Acoustic Impedance'"""
    s = re.sub(r"^\d+\s*[-–]\s*", "", stem)
    s = re.split(r"\s*[｜|]\s*", s, maxsplit=1)[0]
    return s.strip()


def lecture_files(workdir: Path) -> list[Path]:
    return sorted(p for p in workdir.glob("*.txt") if not p.name.endswith(".orig"))

# --------------------------------------------------------------------------- #
#  proofread
# --------------------------------------------------------------------------- #

PROOFREAD_SYS = """\
You are a meticulous transcript proofreader. You receive the raw text of an \
automatically generated (speech-to-text) transcript of a radiology physics lecture, \
one sentence per line, sometimes with no punctuation at all.

Fix ONLY clear, objective errors: misrecognised words and homophones where context \
makes the intended word unambiguous; misheard technical terms (e.g. "Llama frequency" \
-> "Larmor frequency", "ratification" -> "rarefaction", "brem's lung" -> \
"bremsstrahlung", "case space" -> "k-space"); punctuation, capitalisation, sentence \
boundaries, spacing; garbled numbers and units; verbatim stutters. Add sentence-ending \
punctuation and capitalisation where the ASR omitted it.

Do NOT paraphrase, reword, condense, expand, add or remove content, or change meaning \
in any way. Keep roughly one sentence per line. If you cannot determine an intended \
word with confidence, leave it unchanged.

Output ONLY the corrected transcript text: no preamble, no commentary, no code fences."""


def _chunks(text: str, target: int = CHUNK_CHARS) -> list[str]:
    lines, out, buf, size = text.splitlines(keepends=True), [], [], 0
    for ln in lines:
        # hard-wrap very long unpunctuated blobs on spaces
        while len(ln) > target * 2:
            cut = ln.rfind(" ", 0, target * 2) or target * 2
            out.append(ln[:cut]); ln = ln[cut:].lstrip()
        buf.append(ln); size += len(ln)
        if size >= target:
            out.append("".join(buf)); buf, size = [], 0
    if buf:
        out.append("".join(buf))
    return out or [text]


def _sane(a: str, b: str) -> bool:
    na, nb = len(a.split()), len(b.split())
    return nb == 0 if na == 0 else 0.8 <= nb / na <= 1.25


def proofread(workdir: Path, model: str = DEFAULT_MODEL, force: bool = False) -> None:
    client, anthro = make_client()
    files = lecture_files(workdir)
    changed = skipped = failed = 0
    for i, path in enumerate(files, 1):
        backup = path.with_name(path.name + ".orig")
        src = backup if backup.exists() else path
        original = src.read_text(encoding="utf-8")
        if backup.exists() and not force and path.read_text(encoding="utf-8") != original:
            print(f"[{i}/{len(files)}] {path.name}  (done, skip)"); skipped += 1; continue
        print(f"[{i}/{len(files)}] {path.name}")
        try:
            parts = []
            for ch in _chunks(original):
                good = ch.strip("\n")
                for _ in range(2):
                    r = create_with_retry(
                        client, anthro, model=model, max_tokens=16000,
                        system=PROOFREAD_SYS, output_config={"effort": "medium"},
                        messages=[{"role": "user", "content": ch}],
                    )
                    t = "".join(b.text for b in r.content if b.type == "text").strip("\n")
                    if t.startswith("```"):
                        t = t.split("\n", 1)[1].rsplit("```", 1)[0].strip("\n")
                    if _sane(ch, t):
                        good = t; break
                parts.append(good.strip("\n"))
            result = "\n".join(parts).strip("\n") + "\n"
        except anthro.APIError as e:
            print(f"    ! API error, left unchanged: {e}", file=sys.stderr); failed += 1; continue
        if result == original:
            print("    no changes"); continue
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
        path.write_text(result, encoding="utf-8")
        changed += 1
    print(f"[proofread] {changed} changed, {skipped} already done, {failed} failed")

# --------------------------------------------------------------------------- #
#  card generation
# --------------------------------------------------------------------------- #

CARDS_SYS = """\
You are an expert radiologist and Anki deck author. You will be given the transcript \
of one radiology physics lecture. Produce a set of high-yield cloze flashcards.

Rules (follow exactly):
- {n_lo}-{n_hi} cards. Cover the highest-yield, most exam-testable facts and \
relationships only. Skip filler, anecdotes, and channel plugs.
- ONE fact or ONE relationship per card (minimum-information principle). If a concept \
has several parts, make several cards.
- Cloze deletions only. Each `{{{{c1::...}}}}` blank hides ONE or TWO words, never more. \
Most cards have one blank; use c1/c2 only for a genuine pair (e.g. a range, two poles).
- Each card is one short sentence that reads naturally with the blank filled in.
- Correct any speech-to-text errors from the transcript (technical terms, spelling, \
units). The cards must be clean even if the transcript is not.
- No numbered lists inside a card. An arrow "->" may link a cause to its effect where \
that is shorter than a full sentence.
- Assign each card a short "subchapter" label (2-4 words) grouping it within the lecture. \
Reuse the same labels across related cards; keep to ~3-6 labels per lecture.

Output ONLY a JSON array, no prose, no code fences. Each element:
  {{"subchapter": "<label>", "text": "<sentence with {{{{c1::...}}}} cloze>"}}
"""


def _extract_json_array(s: str):
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0]
    i, j = s.find("["), s.rfind("]")
    return json.loads(s[i : j + 1])


def generate_cards(workdir: Path, deck: str, section_prefix: str,
                   model: str = DEFAULT_MODEL, per_lecture: int = 14,
                   reverse: bool = False, out_path: Path | None = None) -> Path:
    client, anthro = make_client()
    files = lecture_files(workdir)
    n = len(files)
    pl_path = workdir / ".playlist.json"
    playlist = json.loads(pl_path.read_text(encoding="utf-8")) if pl_path.exists() else {}
    sys_prompt = CARDS_SYS.format(n_lo=max(6, per_lecture - 3), n_hi=per_lecture + 3)
    decks = []
    for i, path in enumerate(files, 1):
        playlist_idx = int(re.match(r"(\d+)", path.name).group(1))
        course_n = (n + 1 - playlist_idx) if reverse else playlist_idx
        vid = (playlist.get(str(playlist_idx)) or ["", ""])[0]
        title = clean_title(path.stem)
        print(f"[cards] {course_n:02d} {title}")
        transcript = path.read_text(encoding="utf-8")
        cards = None
        for _ in range(2):
            r = create_with_retry(
                client, anthro, model=model, max_tokens=8000,
                system=sys_prompt, output_config={"effort": "high"},
                messages=[{"role": "user",
                           "content": f"Lecture: {title}\n\nTranscript:\n{transcript}"}],
            )
            txt = "".join(b.text for b in r.content if b.type == "text")
            try:
                cards = _extract_json_array(txt)
                cards = [{"subchapter": str(c["subchapter"]), "text": str(c["text"])}
                         for c in cards if "{{c" in c.get("text", "")]
                if cards:
                    break
            except Exception:
                cards = None
        if not cards:
            print(f"    ! could not parse cards for {title}", file=sys.stderr)
            cards = []
        decks.append({
            "n": course_n, "lesson": course_n, "video_id": vid,
            "title": title, "section": f"{section_prefix} - {title}", "cards": cards,
        })
    decks.sort(key=lambda d: d["n"])
    out_path = out_path or (workdir.parent / f"{_slug(deck)}_cards.json")
    out_path.write_text(json.dumps(
        {"parent_deck": deck, "note_type": "Cloze", "decks": decks},
        indent=1, ensure_ascii=False), encoding="utf-8")
    total = sum(len(d["cards"]) for d in decks)
    print(f"[cards] wrote {out_path}  ({total} cards, {len(decks)} lectures)")
    return out_path


# --------------------------------------------------------------------------- #
#  build .apkg
# --------------------------------------------------------------------------- #

def _slug(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", s)).strip("_")


def _stable_id(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest()[:12], 16) % (10 ** 9) + 1_000_000_000


_CARD_CSS = """
.card{font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:20px;color:#1a1d21;background:#fff;text-align:center;line-height:1.5}
.cloze{font-weight:700;color:#1f6f8b}
hr#answer{border:none;border-top:1px solid #d5dae0;margin:14px 0}
.extra{font-size:16px;color:#565d66;margin:6px 0}
.meta{font-size:12px;color:#8b929b;margin-top:14px;line-height:1.6}
.nightMode .card{color:#e6eaed;background:#1a1d21}
.nightMode .cloze{color:#5cb6d1}
.nightMode hr#answer{border-top-color:#2a323a}
.nightMode .extra{color:#9aa2ab}
"""


def build_apkg(cards_json: Path, tag_ns: str, source_tpl: str,
               num_field: str = "n", out_dir: Path | None = None) -> Path:
    import genanki
    data = json.loads(Path(cards_json).read_text(encoding="utf-8"))
    parent = data["parent_deck"]
    model = genanki.Model(
        _stable_id("model:" + parent),
        f"{parent} Cloze (RadiologyTutorials)",
        model_type=genanki.Model.CLOZE,
        fields=[{"name": "Text"}, {"name": "Section"}, {"name": "Source"}, {"name": "Extra"}],
        templates=[{
            "name": "Cloze",
            "qfmt": "{{cloze:Text}}",
            "afmt": ("{{cloze:Text}}<hr id=answer>"
                     "{{#Extra}}<div class=extra>{{Extra}}</div>{{/Extra}}"
                     "<div class=meta>{{Section}}<br>{{Source}}</div>"),
        }],
        css=_CARD_CSS,
    )
    base = _stable_id("deckbase:" + parent)
    decks, n_notes = [], 0
    for d in data["decks"]:
        num = d.get(num_field, d["n"])
        deck = genanki.Deck(base + d["n"], f"{parent}::{d['n']:02d} {d['title']}")
        vid = d.get("video_id") or ""
        url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
        source = f'Radiology Tutorials — "{d["title"]}" ({source_tpl.format(num=num)})'
        if url:
            source += f" · {url}"
        lslug = _slug(d["title"])
        for i, c in enumerate(d["cards"], 1):
            deck.add_note(genanki.Note(
                model=model,
                fields=[c["text"], d["section"], source, ""],
                tags=[f"#RadiologyTutorials::Physics::{tag_ns}::{lslug}::{_slug(c['subchapter'])}"],
                guid=genanki.guid_for(f"{tag_ns.lower()}-{d['n']:02d}-{i:03d}"),
            ))
            n_notes += 1
        decks.append(deck)
    out_dir = Path(out_dir or Path.cwd())
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{parent}.apkg"
    genanki.Package(decks).write_to_file(str(out))
    print(f"[build] wrote {out}  ({len(decks)} subdecks, {n_notes} notes)")
    return out

# --------------------------------------------------------------------------- #
#  orchestration
# --------------------------------------------------------------------------- #

def run(playlist: str, workdir: str, deck: str, tag_ns: str,
        section_prefix: str, source: str, *, reverse: bool = False,
        do_proofread: bool = False, model: str = DEFAULT_MODEL,
        per_lecture: int = 14, skip_download: bool = False) -> None:
    wd = Path(workdir).expanduser().resolve()
    if not skip_download:
        download(playlist, wd)
    if do_proofread:
        proofread(wd, model=model)
    cards_path = wd.parent / f"{_slug(deck)}_cards.json"
    generate_cards(wd, deck, section_prefix, model=model,
                   per_lecture=per_lecture, reverse=reverse, out_path=cards_path)
    build_apkg(cards_path, tag_ns, source, num_field="n", out_dir=wd.parent)


def batch(config: str) -> None:
    for spec in json.loads(Path(config).read_text(encoding="utf-8")):
        print(f"\n=== {spec['deck']} ===")
        run(spec["playlist"], spec["workdir"], spec["deck"], spec["tag_ns"],
            spec.get("section_prefix", spec["deck"] + " Physics"),
            spec.get("source", spec["deck"] + " #{num}"),
            reverse=spec.get("reverse", False),
            do_proofread=spec.get("proofread", False),
            model=spec.get("model", DEFAULT_MODEL),
            per_lecture=spec.get("per_lecture", 14))

# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = lambda sp: (
        sp.add_argument("--deck", required=True, help="parent deck name, e.g. Ultrasound"),
        sp.add_argument("--tag-ns", required=True, help="tag namespace, e.g. Ultrasound"),
        sp.add_argument("--section-prefix", default=None),
        sp.add_argument("--source", default=None, help='e.g. "Ultrasound Physics Course #{num}"'),
        sp.add_argument("--model", default=DEFAULT_MODEL),
        sp.add_argument("--per-lecture", type=int, default=14),
        sp.add_argument("--reverse", action="store_true", help="playlist is in reverse course order"),
    )

    r = sub.add_parser("run", help="full pipeline")
    r.add_argument("playlist"); r.add_argument("workdir")
    common(r)
    r.add_argument("--proofread", action="store_true")
    r.add_argument("--skip-download", action="store_true")

    d = sub.add_parser("download"); d.add_argument("playlist"); d.add_argument("workdir")

    pr = sub.add_parser("proofread"); pr.add_argument("workdir")
    pr.add_argument("--model", default=DEFAULT_MODEL); pr.add_argument("--force", action="store_true")

    c = sub.add_parser("cards"); c.add_argument("workdir")
    common(c); c.add_argument("--out", default=None)

    b = sub.add_parser("build"); b.add_argument("cards_json")
    b.add_argument("--tag-ns", required=True)
    b.add_argument("--source", required=True)
    b.add_argument("--num-field", default="n", choices=["n", "lesson"])
    b.add_argument("--out-dir", default=".")

    ba = sub.add_parser("batch"); ba.add_argument("--config", required=True)

    a = p.parse_args()
    if a.cmd == "run":
        run(a.playlist, a.workdir, a.deck, a.tag_ns,
            a.section_prefix or f"{a.deck} Physics",
            a.source or f"{a.deck} #{{num}}",
            reverse=a.reverse, do_proofread=a.proofread, model=a.model,
            per_lecture=a.per_lecture, skip_download=a.skip_download)
    elif a.cmd == "download":
        download(a.playlist, Path(a.workdir).expanduser().resolve())
    elif a.cmd == "proofread":
        proofread(Path(a.workdir).expanduser().resolve(), model=a.model, force=a.force)
    elif a.cmd == "cards":
        wd = Path(a.workdir).expanduser().resolve()
        generate_cards(wd, a.deck, a.section_prefix or f"{a.deck} Physics",
                       model=a.model, per_lecture=a.per_lecture, reverse=a.reverse,
                       out_path=Path(a.out) if a.out else None)
    elif a.cmd == "build":
        build_apkg(Path(a.cards_json), a.tag_ns, a.source,
                   num_field=a.num_field, out_dir=Path(a.out_dir))
    elif a.cmd == "batch":
        batch(a.config)


if __name__ == "__main__":
    main()
