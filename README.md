# burnaboi: burn it, verify it.

Guided optical disc burning and archive record creation for data discs and true Red Book audio CDs.

`burnaboi` is a Python-based workflow tool for people who still care about optical media as an archival format. It helps you prepare a burn, generate metadata, compute checksums, detect drives, and later verify that a burned disc still matches its original record.

This repo is for the tool itself. Personal disc records should usually live in a separate private repo.

## What it does

- Creates a per-disc record folder
- Generates a `README.txt` burn record from a template
- Computes SHA-256 checksums for payload files
- Detects optical drives
- Burns data discs on Windows
- Burns true Red Book audio CDs on Windows
- Verifies old discs against saved manifests

## Modes

### Data CD/DVD

Data mode prepares a normal filesystem disc and includes:

- `README.txt`
- `checksums.sha256`
- the selected payload files

This is the right mode for archives, backups, photos, documents, software, and general storage.

### Audio CD

Audio mode creates a true Red Book audio CD for standard CD players.

Important constraints:

- Audio mode is CD-only, not DVD
- Audio mode is currently implemented on Windows
- You can burn audio CDs with `burnaboi` by selecting burn mode `1`
- `tools/verify-disc-integrity.py` is not supported for Red Book audio CDs because the format can change track filenames and metadata (song titles and disc label)
- Audio tracks must be converted to 44.1 kHz, 16-bit stereo PCM
- `ffmpeg` is used when conversion is needed
- `README.txt` and `checksums.sha256` stay in the record folder and are not written onto the audio CD

## Why this exists

Optical media is useful for cold storage, offline copies, and long-term preservation, but it is also easy to lose the context of what was burned, when it was burned, and how to verify it years later.

`burnaboi` treats the record as part of the workflow, not an afterthought.

That means every burn can have:

- a human-readable record
- a checksum manifest
- a repeatable verification path
- a durable folder you can keep under version control

## Repository layout

```text
.
├── README.md
├── templates/
│   └── README.template.txt
├── tools/
│   ├── burnaboi.py
│   └── verify-disc-integrity.py
└── examples/
    └── sample-record/
```

## Tested media

- CD-R
- DVD-R (single-layer)
