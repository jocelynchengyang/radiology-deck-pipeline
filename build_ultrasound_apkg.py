#!/usr/bin/env python3
"""
Build Ultrasound.apkg from ultrasound_cards.json.

26 subdecks under a parent "Ultrasound" deck, generic Cloze note type
(fields: Text, Section, Source, Extra), tags per the RadiologyTutorials scheme.

    pip install genanki
    python build_ultrasound_apkg.py
"""

import json
import re
from pathlib import Path

import genanki

HERE = Path(__file__).resolve().parent
DATA = HERE / "ultrasound_cards.json"
OUT = HERE / "Ultrasound.apkg"

MODEL_ID = 1980453117          # fixed, arbitrary
DECK_BASE_ID = 1607392300      # subdeck ids = DECK_BASE_ID + lecture number

MODEL = genanki.Model(
    MODEL_ID,
    "Ultrasound Cloze (RadiologyTutorials)",
    model_type=genanki.Model.CLOZE,
    fields=[
        {"name": "Text"},
        {"name": "Section"},
        {"name": "Source"},
        {"name": "Extra"},
    ],
    templates=[
        {
            "name": "Cloze",
            "qfmt": "{{cloze:Text}}",
            "afmt": (
                "{{cloze:Text}}<hr id=answer>"
                "{{#Extra}}<div class=extra>{{Extra}}</div>{{/Extra}}"
                "<div class=meta>{{Section}}<br>{{Source}}</div>"
            ),
        }
    ],
    css="""
.card{font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:20px;
  color:#1a1d21;background:#fff;text-align:center;line-height:1.5}
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


def slug(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", s)).strip("_")


def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    decks = []
    n_notes = 0

    for d in data["decks"]:
        n = d["n"]
        full_title = d["title"]
        short_title = full_title.split(":")[0].strip()
        deck_name = f"Ultrasound::{n:02d} {short_title}"
        deck = genanki.Deck(DECK_BASE_ID + n, deck_name)

        section = d["section"]  # "Ultrasound Physics - <full title>"
        source = (
            f'Radiology Tutorials — "{full_title}" '
            f'(Radiology Physics Course #{n}) · '
            f'https://www.youtube.com/watch?v={d["video_id"]}'
        )
        lecture_slug = slug(short_title)

        for i, c in enumerate(d["cards"], 1):
            tag = (
                f"#RadiologyTutorials::Physics::Ultrasound::"
                f"{lecture_slug}::{slug(c['subchapter'])}"
            )
            note = genanki.Note(
                model=MODEL,
                fields=[c["text"], section, source, ""],
                tags=[tag],
                guid=genanki.guid_for(f"us-{n:02d}-{i:03d}"),
            )
            deck.add_note(note)
            n_notes += 1

        decks.append(deck)

    genanki.Package(decks).write_to_file(OUT)
    print(f"wrote {OUT}")
    print(f"  {len(decks)} subdecks, {n_notes} notes")


if __name__ == "__main__":
    main()
