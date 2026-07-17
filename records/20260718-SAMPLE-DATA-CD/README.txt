===========================================================
DISC BURN RECORD
===========================================================

Disc Label:         Sample Data CD
Date Burned:        2026-07-18
Burned By:          mdrizwanfk

--- Media Info ---
Disc Type:          Data CD/DVD
Brand/Model:        [e.g. Verbatim MABL]
Burn Speed:          [e.g. 4x]

--- Software / Hardware ---
Burning Software:   [e.g. ImgBurn v2.5.8.0]
Burner Drive:        [e.g. LG WH16NS60]
OS Used:             [e.g. Windows 11 / Ubuntu 24.04]

--- Verification ---
Verify-After-Burn:   [Yes/No]
Checksum Algorithm:  [e.g. SHA-256]
Checksum File:       checksums.sha256
Combined SHA-256:    [Combined SHA-256, to be filled by burnaboi]
Last Re-Verified:    [YYYY-MM-DD] — [Pass/Fail]
                    [Updated automatically by tools/check-disc-integrity.py]

--- Contents Summary ---
 - Burn Mode: Data CD/DVD
 - Payload files: 0
 - Total size: ~0.0 B

--- Notes ---
[Anything worth remembering: source location of original files,
 known issues, why this disc was created, related discs in the
 same set, etc.]

===========================================================
HOW TO VERIFY THIS DISC
===========================================================
1. Copy checksums.sha256 and the disc contents to the same
   working directory (or run the command directly against the
   mounted disc).

2. Re-generate hashes and compare:

   Linux/macOS:
     sha256sum -c checksums.sha256

   Windows (PowerShell):
     Get-FileHash -Algorithm SHA256 * |
       Format-Table -AutoSize
     (then manually compare against checksums.sha256,
      or use a tool like QuickHash / HashCheck)

3. If all files report "OK" (or hashes match), the disc
  contents are intact and unchanged since burning. The repo record's
  `Last Re-Verified` field can then be updated automatically by
  `tools/check-disc-integrity.py`.
===========================================================