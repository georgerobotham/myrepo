# docx-map-updater

Updates a figures section (e.g. an appendix of maps) in a Word report from a
folder of source images: matches each image to an existing figure by
filename/caption similarity, replaces the ones that match, and appends
genuinely new files as new numbered figures — without touching anything else
in the document.

Runs as a small local web app. Nothing leaves your machine: the server only
listens on `127.0.0.1`, so it's not reachable from the network, and no
external services are called.

## Running it

**From source:**

```bash
pip install -r requirements.txt
python webapp.py
```

This opens `http://127.0.0.1:8877` in your browser.

**Windows, no Python needed:** build a standalone `.exe` with
`pyinstaller webapp.spec` (see [Files](#files) below) and double-click the
result in `dist/`. Same behaviour — your browser opens automatically to the
tool.

## How to use it

1. Drop your Word report (`.docx`) and a folder of source images onto the
   page.
2. Pick the section of the report to update from the dropdown (it's built
   from the document's own headings).
3. Optionally add a general caption style/note — it gets folded into
   suggested captions for any new figures.
4. Click **Preview** to see what would happen: which figures get replaced,
   which files become new figures, which existing figures have no match, and
   any conflicts (two files scoring similarly against the same figure — these
   are always left alone, not guessed).
5. Review and edit the suggested captions for any new figures.
6. Click **Apply & Download** to get the updated `.docx` and a JSON report of
   everything that changed.

The original file is never modified — you always get a new file back.

## How matching works

Filenames and existing figure captions are tokenized and scored with a blend
of token overlap and string similarity. Matches at or above the threshold
(default `0.55`, adjustable) are treated as replacements; everything else
becomes a new figure. Figure numbering is scoped to the section you picked,
not the whole document, matching how numbering usually resets per
appendix/section in long reports.

## Files

- `engine.py` — the core logic (no GUI dependency): finds the section, parses
  existing figures, matches files, applies changes, and validates the saved
  file's OOXML ID attributes (a real Word compatibility requirement — see the
  comments in `engine.py` if you're touching that code).
- `webapp.py` + `templates/index.html` — the local web UI (Flask).
- `docx_map_updater.py` — an earlier desktop (tkinter) version with
  drag-and-drop but without the section picker/caption review features. Kept
  for reference; the web version is the maintained one.
- `test_fixture.py` — builds a synthetic test document and images, runs the
  engine end-to-end, and independently verifies the result (pixel-samples the
  output images, checks section boundaries were respected, validates OOXML
  IDs). Run with `python test_fixture.py`.
- `*.spec` — PyInstaller specs for building the standalone `.exe`s
  (`pyinstaller webapp.spec`).

## Requirements

Python 3.10+, `python-docx`, `Pillow`, `Flask`. See `requirements.txt`.
