# radiology-deck-pipeline

Turn a YouTube lecture playlist into an Anki `.apkg` of cloze flashcards.

Built for the "Radiology Physics Course" series on YouTube (ultrasound, MRI,
X-ray, CT), but the pipeline is generic — point it at any playlist of
lecture videos with auto-generated captions.

```
playlist URL
  -> download auto-captions (yt-dlp)                    [download]
  -> clean to one-sentence-per-line .txt
  -> optional proofread with Claude (resumable)          [proofread]
  -> generate ~10-15 high-yield cloze cards per lecture   [cards]     (Claude)
  -> build <Deck>.apkg (genanki)                          [build]
```

## Requirements

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-api03-...
```

(On macOS, the key can instead live in the Keychain under the name
`anthropic_api_key`, e.g. `security add-generic-password -a "$USER" -s anthropic_api_key -w sk-ant-...`.)

## Usage

Full pipeline for one playlist:

```
python radiology_deck_pipeline.py run "PLAYLIST_URL" ./ct \
  --deck CT --tag-ns CT --section-prefix "CT Physics" \
  --source "CT Physics Course Lesson #{num}" \
  --reverse --proofread
```

`--reverse` is for playlists that are in reverse course order (lesson N
first). `--per-lecture N` sets the target card count per lecture (default 14).

Individual steps: `download`, `proofread`, `cards`, `build` — run
`python radiology_deck_pipeline.py <command> -h` for each command's options.

Batch mode runs several playlists from one config file:

```
python radiology_deck_pipeline.py batch --config decks.json
```

See `decks.json` for the config shape (one object per deck).

## Card format

Every generated card:

- targets one fact or one relationship (minimum-information principle)
- hides one or two words per cloze blank
- is tagged `#RadiologyTutorials::Physics::<Series>::<Lecture>::<Subchapter>`
- carries a `Section` field (`"<Series> Physics - <lecture title>"`) and a
  `Source` field (lecture title, course number, video URL) on the back

The Anki note type is a generic **Cloze** with fields `Text`, `Section`,
`Source`, `Extra` — re-map to a different note type on import if needed.

## Other scripts

- `build_apkg.py` — generic, config-driven `cards.json -> .apkg` builder
  used by the `build` subcommand.
- `build_ultrasound_apkg.py`, `build_mri_apkg.py` — earlier one-off builders
  for those two decks, kept for reference; `build_apkg.py` supersedes them.
- `proofread_transcripts.py` — standalone transcript proofreader (the logic
  is folded into `radiology_deck_pipeline.py proofread`, kept here as a
  simpler single-purpose script).

## What's not in this repo

Transcripts, generated `cards.json` files, and `.apkg` decks are not
included — they're derived from someone else's lecture content, so
republishing them here isn't appropriate. This repo is the tooling only;
run it against a playlist you have the right to build study material from.

## License

MIT — see [LICENSE](LICENSE).
