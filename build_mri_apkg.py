#!/usr/bin/env python3
"""
Build MRI.apkg from mri_cards.json.

28 subdecks under a parent "MRI" deck, generic Cloze note type
(fields: Text, Section, Source, Extra), full lecture titles as deck names.

    pip install genanki
    python build_mri_apkg.py
"""

import json
import re
from pathlib import Path

import genanki

HERE = Path(__file__).resolve().parent
DATA = HERE / "mri_cards.json"
OUT = HERE / "MRI.apkg"

MODEL_ID = 1980453222
DECK_BASE_ID = 1607399100  # subdeck ids = DECK_BASE_ID + lecture number

MODEL = genanki.Model(
    MODEL_ID,
    "MRI Cloze (RadiologyTutorials)",
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
  color:#1b1a20;background:#fff;text-align:center;line-height:1.5}
.cloze{font-weight:700;color:#4f46a8}
.arrow{color:#4f46a8;font-weight:700}
hr#answer{border:none;border-top:1px solid #d8d7e0;margin:14px 0}
.extra{font-size:16px;color:#585766;margin:6px 0}
.meta{font-size:12px;color:#8d8b9b;margin-top:14px;line-height:1.6}
.nightMode .card{color:#e8e7ee;background:#1b1a20}
.nightMode .cloze,.nightMode .arrow{color:#9d95e0}
.nightMode hr#answer{border-top-color:#2e2c3c}
.nightMode .extra{color:#a09eb0}
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
        title = d["title"]
        deck = genanki.Deck(DECK_BASE_ID + n, f"MRI::{n:02d} {title}")

        section = d["section"]  # "MRI Physics - <title>"
        source = (
            f'Radiology Tutorials — "{title}" '
            f'(Radiology Physics Course #{n}) · '
            f'https://www.youtube.com/watch?v={d["video_id"]}'
        )
        lecture_slug = slug(title)

        for i, c in enumerate(d["cards"], 1):
            tag = (
                f"#RadiologyTutorials::Physics::MRI::"
                f"{lecture_slug}::{slug(c['subchapter'])}"
            )
            deck.add_note(
                genanki.Note(
                    model=MODEL,
                    fields=[c["text"], section, source, ""],
                    tags=[tag],
                    guid=genanki.guid_for(f"mri-{n:02d}-{i:03d}"),
                )
            )
            n_notes += 1

        decks.append(deck)

    genanki.Package(decks).write_to_file(OUT)
    print(f"wrote {OUT}")
    print(f"  {len(decks)} subdecks, {n_notes} notes")


if __name__ == "__main__":
    main()
