#!/usr/bin/env python3
"""
Generic builder: <cards>.json  ->  <Deck>.apkg

Usage:
    python build_apkg.py xray_cards.json  X-ray  "X-ray Physics"  Xray  "X-ray Physics Course #{num}"  n
    python build_apkg.py ct_cards.json    CT     "CT Physics"     CT    "CT Physics Course Lesson #{num}" lesson

Args: json_path  parent_deck  section_prefix  tag_namespace  source_template  num_field
(num_field is "n" or "lesson" — which JSON field fills {num} in the source line)

Attribution in each card's Source field comes from the "channel" key in
cards.json (written by radiology_deck_pipeline.py's `cards` step from the
playlist's own channel name) if present, otherwise the Source field just
quotes the lecture title. Tags are "#<tag_namespace>::<lecture>::<subchapter>"
with no forced prefix — this script has no notion of a specific series.

Requires: genanki
"""
import json, re, sys, hashlib
from pathlib import Path
import genanki

HERE = Path(__file__).resolve().parent


def slug(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", s)).strip("_")


def stable_id(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest()[:12], 16) % (10 ** 9) + 1_000_000_000


def build(json_path, parent, section_prefix, tag_ns, source_tpl, num_field):
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    channel = data.get("channel", "")
    model = genanki.Model(
        stable_id("model:" + parent),
        f"{parent} Cloze",
        model_type=genanki.Model.CLOZE,
        fields=[{"name": "Text"}, {"name": "Section"}, {"name": "Source"}, {"name": "Extra"}],
        templates=[{
            "name": "Cloze",
            "qfmt": "{{cloze:Text}}",
            "afmt": ("{{cloze:Text}}<hr id=answer>"
                     "{{#Extra}}<div class=extra>{{Extra}}</div>{{/Extra}}"
                     "<div class=meta>{{Section}}<br>{{Source}}</div>"),
        }],
        css="""
.card{font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:20px;color:#1a1d21;background:#fff;text-align:center;line-height:1.5}
.cloze{font-weight:700;color:#1f6f8b}
.arrow{color:#1f6f8b;font-weight:700}
hr#answer{border:none;border-top:1px solid #d5dae0;margin:14px 0}
.extra{font-size:16px;color:#565d66;margin:6px 0}
.meta{font-size:12px;color:#8b929b;margin-top:14px;line-height:1.6}
.nightMode .card{color:#e6eaed;background:#1a1d21}
.nightMode .cloze,.nightMode .arrow{color:#5cb6d1}
.nightMode hr#answer{border-top-color:#2a323a}
.nightMode .extra{color:#9aa2ab}
""",
    )
    base_deck_id = stable_id("deckbase:" + parent)
    decks, n_notes = [], 0
    for d in data["decks"]:
        n = d["n"]
        title = d["title"]
        deck = genanki.Deck(base_deck_id + n, f"{parent}::{n:02d} {title}")
        section = d.get("section") or f"{section_prefix} - {title}"
        num = d[num_field]
        title_part = f'"{title}" ({source_tpl.format(num=num)})'
        source = (f"{channel} — {title_part}" if channel else title_part) + \
                 f' · https://www.youtube.com/watch?v={d["video_id"]}'
        lecture_slug = slug(title)
        for i, c in enumerate(d["cards"], 1):
            tag = f"#{tag_ns}::{lecture_slug}::{slug(c['subchapter'])}"
            deck.add_note(genanki.Note(
                model=model,
                fields=[c["text"], section, source, ""],
                tags=[tag],
                guid=genanki.guid_for(f"{tag_ns.lower()}-{n:02d}-{i:03d}"),
            ))
            n_notes += 1
        decks.append(deck)
    out = HERE / f"{parent}.apkg"
    genanki.Package(decks).write_to_file(out)
    print(f"wrote {out}  ({len(decks)} subdecks, {n_notes} notes)")


if __name__ == "__main__":
    build(*sys.argv[1:7])
