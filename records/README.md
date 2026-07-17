# Burnaboi - Records Directory

This `records/` directory contains archival metadata for discs you've created with `burnaboi.py`.

What you'll find here

- Per-burn record folders named `YYYYMMDD-DISC_LABEL/` (e.g. `20260718-SAMPLE-AUDIO-CD/`).
- Each record folder includes `README.txt`, `checksums.sha256`, and a `content/` subfolder holding the original files (if retained).

Privacy & security

- These files may contain private information (disc labels, contents, checksums). Treat this directory as sensitive.
- To remove local traces, delete a specific record folder (e.g. `rm -rf records/20260718-...`).
- To stop tracking future records in Git, add `records/` to your `.gitignore`.

Verification commands

- Linux/macOS (from the mounted disc or copied files):

  sha256sum -c checksums.sha256

- Windows PowerShell (list hashes and compare manually):

  Get-FileHash -Algorithm SHA256 \* | Format-Table -AutoSize
