#!/usr/bin/env python3
import re
import sys
import shutil
import os
import hashlib
import getpass
import json
import platform
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from datetime import date


TEXT_ENCODING = "utf-8"
AUDIO_DISC_MODE = "audio"
DATA_DISC_MODE = "data"
COMMON_AUDIO_EXTENSIONS = {
    ".aac",
    ".ac3",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".mp2",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
RED_BOOK_SAMPLE_RATE = 44100
RED_BOOK_CHANNELS = 2
RED_BOOK_SAMPLE_WIDTH_BYTES = 2
RED_BOOK_BYTES_PER_SECOND = RED_BOOK_SAMPLE_RATE * RED_BOOK_CHANNELS * RED_BOOK_SAMPLE_WIDTH_BYTES
RED_BOOK_SECTOR_BYTES = 2352
RED_BOOK_MAX_SECONDS = 80 * 60  # 80-minute CD nominal maximum
IMAPI_MEDIA_TYPE_UNKNOWN = 0x0
IMAPI_MEDIA_TYPE_CDROM = 0x1
IMAPI_MEDIA_TYPE_CDR = 0x2
IMAPI_MEDIA_TYPE_CDRW = 0x3


MEDIA_TYPE_NAMES = {
    IMAPI_MEDIA_TYPE_UNKNOWN: "Unknown / no media",
    IMAPI_MEDIA_TYPE_CDROM: "CD-ROM or read-only CD",
    IMAPI_MEDIA_TYPE_CDR: "CD-R",
    IMAPI_MEDIA_TYPE_CDRW: "CD-RW",
    0x4: "DVD-ROM",
    0x5: "DVD-RAM",
    0x6: "DVD+R",
    0x7: "DVD+RW",
    0x8: "DVD+R DL",
    0x9: "DVD-R",
    0xA: "DVD-RW",
    0xB: "DVD-R DL",
    0xC: "Random-access disc",
    0xD: "DVD+RW DL",
    0xE: "HD DVD-ROM",
    0xF: "HD DVD-R",
    0x10: "HD DVD-RAM",
    0x11: "BD-ROM",
    0x12: "BD-R",
    0x13: "BD-RE",
}


def configure_stdio() -> None:
    """Prefer UTF-8 console output and avoid crashes on non-UTF-8 Windows streams."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            continue


configure_stdio()


def read_text_file(path: Path) -> str:
    """Read a UTF-8 text file."""
    with open(path, "r", encoding=TEXT_ENCODING) as file_handle:
        return file_handle.read()


def write_text_file(path: Path, content: str) -> None:
    """Write a UTF-8 text file."""
    with open(path, "w", encoding=TEXT_ENCODING) as file_handle:
        file_handle.write(content)


def prompt_disc_label() -> str:
    while True:
        label = input("Disc Label: ").strip()
        if label:
            return label
        print("Error: Disc label cannot be empty.", file=sys.stderr)


def prompt_burned_by() -> str:
    try:
        default = getpass.getuser()
    except Exception:
        default = "Unknown"
    value = input(f"Burned By [{default}]: ").strip()
    return value if value else default


def prompt_burn_mode() -> str:
    while True:
        print("Burn Mode:")
        print("[1] Audio CD (Red Book, CD only)")
        print("[2] Data CD/DVD")
        choice = input("Select mode [1/2]: ").strip()
        if choice == "1":
            return AUDIO_DISC_MODE
        if choice == "2":
            return DATA_DISC_MODE
        print("Invalid selection. Please choose 1 or 2.", file=sys.stderr)


def sanitize_label_for_folder(label: str) -> str:
    label = label.upper()
    label = label.replace(" ", "-")
    label = re.sub(r"[^A-Z0-9\-_]", "", label)
    label = re.sub(r"-{2,}", "-", label)
    label = label.strip("-_")
    return label


def sanitize_volume_label(label: str) -> str:
    """Keep the entered disc label readable while removing unsupported filesystem characters."""
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", label)
    sanitized = re.sub(r"\s+", " ", sanitized).strip().rstrip(".")
    return sanitized or "DISC"


def create_folders(records_dir: Path, folder_name: str) -> Path:
    """Create main folder and content subfolder without replacing existing content."""
    folder_path = records_dir / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)
    (folder_path / "content").mkdir(exist_ok=True)
    return folder_path


def get_all_content_files(content_folder: Path) -> list[Path]:
    return sorted(path for path in content_folder.rglob("*") if path.is_file())


def is_supported_audio_file(path: Path) -> bool:
    return path.suffix.lower() in COMMON_AUDIO_EXTENSIONS


def get_selected_payload_files(content_folder: Path, burn_mode: str) -> list[Path]:
    content_files = get_all_content_files(content_folder)
    if burn_mode == AUDIO_DISC_MODE:
        return [path for path in content_files if is_supported_audio_file(path)]
    return content_files


def format_burn_mode_label(burn_mode: str) -> str:
    if burn_mode == AUDIO_DISC_MODE:
        return "Audio CD (Red Book)"
    return "Data CD/DVD"


def format_audio_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{remaining_seconds:02d}"


def get_ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def get_media_duration_seconds(path: Path, ffmpeg_path: str | None = None, ffprobe_path: str | None = None) -> float:
    """Return media duration in seconds or -1.0 if unknown.

    Tries WAV via wave module, then ffprobe, then ffmpeg parsing fallback.
    """
    # WAV fast path
    try:
        if path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as wav_file:
                frames = wav_file.getnframes()
                rate = wav_file.getframerate()
                if rate > 0:
                    return frames / float(rate)
    except Exception:
        pass

    # ffprobe if available
    probe = ffprobe_path or shutil.which("ffprobe")
    if probe:
        code, stdout, stderr = run_command([probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
        if code == 0 and stdout.strip():
            try:
                return float(stdout.strip())
            except Exception:
                pass

    # ffmpeg -i parse stderr
    ff = ffmpeg_path or get_ffmpeg_path()
    if ff:
        code, stdout, stderr = run_command([ff, "-i", str(path)])
        out = stderr or stdout
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
        if m:
            hours = int(m.group(1))
            minutes = int(m.group(2))
            seconds = float(m.group(3))
            return hours * 3600 + minutes * 60 + seconds

    return -1.0


def get_red_book_duration_from_raw_size(num_bytes: int) -> float:
    return num_bytes / RED_BOOK_BYTES_PER_SECOND


def validate_red_book_wav(source_path: Path) -> tuple[bool, str]:
    try:
        with wave.open(str(source_path), "rb") as wav_file:
            if wav_file.getnchannels() != RED_BOOK_CHANNELS:
                return False, "must be stereo"
            if wav_file.getsampwidth() != RED_BOOK_SAMPLE_WIDTH_BYTES:
                return False, "must be 16-bit PCM"
            if wav_file.getframerate() != RED_BOOK_SAMPLE_RATE:
                return False, "must be 44.1 kHz"
            if wav_file.getcomptype() != "NONE":
                return False, "must be uncompressed PCM"
    except wave.Error as error:
        return False, f"invalid WAV file: {error}"
    return True, ""


def prepare_red_book_track(source_path: Path, prepared_dir: Path, ffmpeg_path: str | None) -> dict[str, str | float | Path]:
    # Include parent folder context to avoid filename collisions when stems repeat.
    parent_slug = re.sub(r"[^A-Za-z0-9_-]", "_", source_path.parent.name)
    raw_target = prepared_dir / f"{parent_slug}__{source_path.stem}.raw"

    if ffmpeg_path:
        command = [
            ffmpeg_path,
            "-y",
            "-v",
            "error",
            "-i",
            str(source_path),
            "-f",
            "s16le",
            "-ar",
            str(RED_BOOK_SAMPLE_RATE),
            "-ac",
            str(RED_BOOK_CHANNELS),
            str(raw_target),
        ]
        code, stdout, stderr = run_command(command)
        if code != 0:
            detail = stderr.strip() or stdout.strip() or "ffmpeg conversion failed"
            raise RuntimeError(f"Failed to convert {source_path.name}: {detail}")
    else:
        if source_path.suffix.lower() != ".wav":
            raise RuntimeError(
                f"{source_path.name} requires ffmpeg for Audio CD conversion. Install ffmpeg or use WAV files already in 44.1 kHz, 16-bit stereo PCM format."
            )

        is_valid, reason = validate_red_book_wav(source_path)
        if not is_valid:
            raise RuntimeError(
                f"{source_path.name} cannot be used without ffmpeg because it {reason}. Install ffmpeg or convert it to 44.1 kHz, 16-bit stereo PCM WAV first."
            )

        with wave.open(str(source_path), "rb") as wav_file, open(raw_target, "wb") as raw_file:
            raw_file.write(wav_file.readframes(wav_file.getnframes()))

    raw_size = raw_target.stat().st_size
    if raw_size <= 0:
        raise RuntimeError(f"{source_path.name} converted to an empty PCM stream.")

    # IMAPI Track-At-Once expects audio payload sizes aligned to 2352-byte CD sectors.
    remainder = raw_size % RED_BOOK_SECTOR_BYTES
    if remainder != 0:
        pad_bytes = RED_BOOK_SECTOR_BYTES - remainder
        with open(raw_target, "ab") as raw_file:
            raw_file.write(b"\x00" * pad_bytes)
        raw_size += pad_bytes

    duration_seconds = get_red_book_duration_from_raw_size(raw_size)
    return {
        "source_path": source_path,
        "raw_path": raw_target,
        "raw_size": raw_size,
        "duration_seconds": duration_seconds,
    }


def prepare_red_book_tracks(content_folder: Path, prepared_dir: Path) -> list[dict[str, str | float | Path]]:
    source_files = get_selected_payload_files(content_folder, AUDIO_DISC_MODE)
    if not source_files:
        return []

    ffmpeg_path = get_ffmpeg_path()
    prepared_tracks = []
    print(f"Preparing Red Book audio tracks ({len(source_files)} track{'s' if len(source_files) != 1 else ''})...")
    for index, source_path in enumerate(source_files, 1):
        track_info = prepare_red_book_track(source_path, prepared_dir, ffmpeg_path)
        prepared_tracks.append(track_info)
        duration_text = format_audio_duration(float(track_info["duration_seconds"]))
        print(f"  [{index}/{len(source_files)}] {source_path.name:<50} {duration_text}")

    return prepared_tracks


def get_media_type_name(media_type_code: int) -> str:
    return MEDIA_TYPE_NAMES.get(media_type_code, f"Unknown media type ({media_type_code})")


def get_windows_media_type_for_drive(selected_drive: dict[str, str]) -> tuple[int, str]:
    drive_path = selected_drive["path"]
    if drive_path == "Unavailable":
        raise RuntimeError("Selected drive does not expose a usable drive path.")

    escaped_drive = drive_path.rstrip("\\") + "\\"
    escaped_drive = escaped_drive.replace("'", "''")
    powershell_script = f"""
$ErrorActionPreference = 'Stop'
$drivePath = '{escaped_drive}'
$master = New-Object -ComObject IMAPI2.MsftDiscMaster2
$recorder = $null
foreach ($id in @($master)) {{
    $candidate = New-Object -ComObject IMAPI2.MsftDiscRecorder2
    $candidate.InitializeDiscRecorder($id)
    if ($candidate.VolumePathNames -contains $drivePath) {{
        $recorder = $candidate
        break
    }}
}}
if ($null -eq $recorder) {{
    throw 'No matching recorder found for selected drive.'
}}
$format = New-Object -ComObject IMAPI2.MsftDiscFormat2TrackAtOnce
$format.Recorder = $recorder
[pscustomobject]@{{ MediaType = [int]$format.CurrentPhysicalMediaType }} | ConvertTo-Json -Compress
"""

    code, stdout, stderr = run_command([
        "powershell",
        "-NoProfile",
        "-Command",
        powershell_script,
    ])
    if code != 0:
        detail = stderr.strip() or stdout.strip() or "Unable to determine inserted media type"
        raise RuntimeError(detail)

    try:
        payload = json.loads(stdout.strip())
        media_type_code = int(payload.get("MediaType", IMAPI_MEDIA_TYPE_UNKNOWN))
    except (ValueError, json.JSONDecodeError, AttributeError):
        raise RuntimeError("Unable to determine inserted media type from IMAPI.")

    return media_type_code, get_media_type_name(media_type_code)


def ensure_audio_cd_media_is_writable(selected_drive: dict[str, str]) -> tuple[int, str]:
    media_type_code, media_type_name = get_windows_media_type_for_drive(selected_drive)
    if media_type_code not in {IMAPI_MEDIA_TYPE_CDR, IMAPI_MEDIA_TYPE_CDRW}:
        raise RuntimeError(
            f"Audio CD mode requires writable CD-R or CD-RW media. Detected: {media_type_name}."
        )
    return media_type_code, media_type_name


def describe_selected_payload(content_folder: Path, burn_mode: str, selected_files: list[Path]) -> str:
    total_bytes = sum(path.stat().st_size for path in selected_files)
    lines = [f" - Burn Mode: {format_burn_mode_label(burn_mode)}"]

    if burn_mode == AUDIO_DISC_MODE:
        all_files = get_all_content_files(content_folder)
        skipped_files = [path for path in all_files if path not in selected_files]
        lines.append(f" - Audio files selected: {len(selected_files)}")
        lines.append(f" - Non-audio files excluded: {len(skipped_files)}")
        lines.append(" - README.txt and checksums.sha256 stay in the record folder; true audio CDs do not contain disc-root data files.")
        if selected_files:
            lines.append(" - Included audio files:")
            for path in selected_files:
                lines.append(f"   - {path.relative_to(content_folder).as_posix()}")
    else:
        lines.append(f" - Payload files: {len(selected_files)}")
        if selected_files:
            lines.append(" - Included files:")
            for path in selected_files:
                lines.append(f"   - {path.relative_to(content_folder).as_posix()}")

    lines.append(f" - Total size: ~{format_bytes(total_bytes)}")
    return "\n".join(lines)


def clear_content_folder(content_folder: Path) -> tuple[int, int]:
    """Delete the content folder and everything inside it."""
    removed_files = 0
    removed_dirs = 0

    for file_path in sorted(
        (path for path in content_folder.rglob("*") if path.is_file()),
        reverse=True,
    ):
        file_path.unlink()
        removed_files += 1

    for dir_path in sorted(
        (path for path in content_folder.rglob("*") if path.is_dir()),
        reverse=True,
    ):
        dir_path.rmdir()
        removed_dirs += 1

    if content_folder.exists():
        content_folder.rmdir()
        removed_dirs += 1

    return removed_files, removed_dirs


def populate_and_write_readme(
    folder_path: Path,
    disc_label: str,
    burned_by: str,
    burn_date: str,
    burn_mode: str,
    content_folder: Path,
) -> Path:
    """Read template, populate fields, and write README to folder."""
    template_path = Path("templates/README.template.txt")
    
    if not template_path.exists():
        print(f"Error: Template not found at {template_path}", file=sys.stderr)
        sys.exit(1)
    
    content = read_text_file(template_path)
    selected_files = get_selected_payload_files(content_folder, burn_mode)
    contents_summary = describe_selected_payload(content_folder, burn_mode, selected_files)
    
    # Replace placeholders with actual values
    content = content.replace("[e.g. BACKUP-2026-07-PHOTOS-01]", disc_label)
    content = content.replace("[YYYY-MM-DD]", burn_date, 1)  # Only replace first occurrence (Disc Label section)
    content = content.replace("[Your name / handle]", burned_by)
    content = content.replace("[e.g. BD-R 25GB / DVD+R 4.7GB]", format_burn_mode_label(burn_mode))
    content = content.replace(
        "[Short description of what's on this disc, e.g.:\n - Family photos, Jan-Jun 2026\n - 3 subfolders: Trip1/, Trip2/, Scans/\n - Total size: ~4.2 GB, 812 files]",
        contents_summary,
    )
    
    readme_path = folder_path / "README.txt"
    write_text_file(readme_path, content)
    
    return readme_path


def refresh_readme(folder_path: Path, disc_label: str, burned_by: str, burn_date: str, burn_mode: str, content_folder: Path) -> None:
    populate_and_write_readme(folder_path, disc_label, burned_by, burn_date, burn_mode, content_folder)


def compute_sha256_file(filepath: Path) -> str:
    """Compute SHA256 checksum of a file."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def run_command(command: list[str]) -> tuple[int, str, str]:
    """Run a command and return code, stdout, and stderr."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def is_likely_virtual_drive(text: str) -> bool:
    """Best-effort heuristic to filter virtual optical devices."""
    virtual_keywords = [
        "virtual",
        "daemon",
        "clone",
        "vbox",
        "vmware",
        "hyper-v",
        "alcohol",
        "magiciso",
    ]
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in virtual_keywords)


def format_bytes(num_bytes: int) -> str:
    """Format bytes in a human-readable form."""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{num_bytes} B"


def format_elapsed_time(seconds: float) -> str:
    """Format elapsed time in a human-readable form."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}m"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}h"


def get_free_space_for_path(path_value: str) -> str:
    """Return free space for a mounted path if available."""
    # Normalize common Windows drive-root forms (E:/, E:\\, E:) to a proper root path
    def _normalize_path(p: str) -> str:
        if not p:
            return p
        if platform.system() == "Windows":
            m = re.match(r"^([A-Za-z]):[\\/]*$", p)
            if m:
                return m.group(1) + ":\\"
        return p

    try:
        usage = shutil.disk_usage(_normalize_path(path_value))
        return format_bytes(usage.free)
    except OSError as e:
        msg = str(e).lower()
        if platform.system() == "Windows" and ("device is not ready" in msg or "not ready" in msg):
            return "No writable media"
        return "Unavailable"


def get_free_bytes_for_path(path_value: str) -> int:
    """Return raw free bytes for a path, or -1 if unavailable."""
    def _normalize_path(p: str) -> str:
        if not p:
            return p
        if platform.system() == "Windows":
            m = re.match(r"^([A-Za-z]):[\\/]*$", p)
            if m:
                return m.group(1) + ":\\"
        return p

    try:
        return shutil.disk_usage(_normalize_path(path_value)).free
    except OSError:
        return -1


def detect_optical_drives_windows() -> list[dict[str, str]]:
    """Detect physical optical drives on Windows using CIM."""
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_CDROMDrive | ForEach-Object { "
            "[pscustomobject]@{ "
            "Drive=$_.Drive; "
            "Caption=$_.Caption; "
            "Manufacturer=$_.Manufacturer; "
            "MediaLoaded=[bool]$_.MediaLoaded; "
            "CapabilityDescriptions=($_.CapabilityDescriptions -join '; ') "
            "} } | ConvertTo-Json -Compress"
        ),
    ]
    code, stdout, _ = run_command(command)
    if code != 0:
        return []

    payload = stdout.strip()
    if not payload:
        return []

    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return []

    if isinstance(raw, dict):
        raw = [raw]

    drives = []
    for item in raw:
        caption = str(item.get("Caption") or "Unknown")
        manufacturer = str(item.get("Manufacturer") or "")
        combined_text = f"{caption} {manufacturer}"
        if is_likely_virtual_drive(combined_text):
            continue

        drive_letter = str(item.get("Drive") or "")
        media_loaded = bool(item.get("MediaLoaded"))
        caps = str(item.get("CapabilityDescriptions") or "")
        caps_lower = caps.lower()
        if "write" in caps_lower or "writing" in caps_lower or "record" in caps_lower or "burn" in caps_lower:
            burn_capable = "Yes"
        elif caps:
            burn_capable = "No/Unknown"
        else:
            burn_capable = "Unknown"

        if media_loaded and drive_letter:
            probe_path = f"{drive_letter}\\"
            free_space = get_free_space_for_path(probe_path)
            free_space_bytes = get_free_bytes_for_path(probe_path)

            # If disk_usage couldn't provide values (e.g. blank disc: "No writable media"),
            # try an IMAPI probe to decide whether the media is a blank writable disc.
            if (isinstance(free_space, str) and free_space in ("Unavailable", "No writable media")) or free_space_bytes == -1:
                try:
                    temp_drive = {"path": drive_letter}
                    media_type_code, media_type_name = get_windows_media_type_for_drive(temp_drive)
                    # If it's writable media (CD-R or CD-RW or similar), label as blank/unformatted.
                    if media_type_code in {IMAPI_MEDIA_TYPE_CDR, IMAPI_MEDIA_TYPE_CDRW}:
                        free_space = f"Blank {media_type_name} (no filesystem)"
                        free_space_bytes = -1
                except RuntimeError:
                    # Fall back to previous unavailable indicators
                    if isinstance(free_space, str) and free_space != "No writable media":
                        free_space = "Unavailable"
                    free_space_bytes = -1
        elif media_loaded:
            free_space = "Unavailable"
            free_space_bytes = -1
        else:
            free_space = "No writable media"
            free_space_bytes = -1

        drives.append(
            {
                "path": drive_letter or "Unavailable",
                "model": caption,
                "media_loaded": "Yes" if media_loaded else "No",
                "burn_capable": burn_capable,
                "free_space": free_space,
                "free_space_bytes": str(free_space_bytes),
            }
        )

    return drives


def detect_optical_drives_linux() -> list[dict[str, str]]:
    """Detect optical drives on Linux via /sys and mounted paths."""
    drives = []
    block_root = Path("/sys/class/block")
    if not block_root.exists():
        return drives

    for entry in sorted(block_root.iterdir()):
        dev_type = entry / "device" / "type"
        if not dev_type.exists():
            continue

        try:
            if dev_type.read_text(encoding="utf-8").strip() != "5":
                continue
        except OSError:
            continue

        model_path = entry / "device" / "model"
        vendor_path = entry / "device" / "vendor"
        model = " ".join(
            x.strip()
            for x in [
                vendor_path.read_text(encoding="utf-8").strip() if vendor_path.exists() else "",
                model_path.read_text(encoding="utf-8").strip() if model_path.exists() else "",
            ]
            if x.strip()
        )
        if is_likely_virtual_drive(model):
            continue

        device_path = f"/dev/{entry.name}"
        mount_path = ""
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as mounts_file:
                for line in mounts_file:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == device_path:
                        mount_path = parts[1]
                        break
        except OSError:
            mount_path = ""

        media_loaded = "Yes" if mount_path else "No"
        free_space = get_free_space_for_path(mount_path) if mount_path else "No writable media"
        free_space_bytes = get_free_bytes_for_path(mount_path) if mount_path else -1

        drives.append(
            {
                "path": mount_path or device_path,
                "model": model or entry.name,
                "media_loaded": media_loaded,
                "burn_capable": "Unknown",
                "free_space": free_space,
                "free_space_bytes": str(free_space_bytes),
            }
        )

    return drives


def detect_optical_drives_macos() -> list[dict[str, str]]:
    """Detect optical drives on macOS using drutil (best-effort)."""
    code, stdout, _ = run_command(["drutil", "status"])
    if code != 0:
        return []

    if "No drives found" in stdout:
        return []

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    model = "Unknown"
    media_loaded = "No"
    for line in lines:
        if line.lower().startswith("vendor") or line.lower().startswith("product"):
            model = line
        if "media" in line.lower() and "present" in line.lower():
            media_loaded = "Yes"

    return [
        {
            "path": "Optical Drive",
            "model": model,
            "media_loaded": media_loaded,
            "burn_capable": "Unknown",
            "free_space": "Unsupported on this OS",
            "free_space_bytes": "-1",
        }
    ]


def detect_optical_drives() -> list[dict[str, str]]:
    """Cross-platform optical drive discovery."""
    system_name = platform.system()
    print("Detecting optical drives...", end=" ", flush=True)
    start_time = time.time()
    
    if system_name == "Windows":
        drives = detect_optical_drives_windows()
    elif system_name == "Linux":
        drives = detect_optical_drives_linux()
    elif system_name == "Darwin":
        drives = detect_optical_drives_macos()
    else:
        drives = []
    
    elapsed = time.time() - start_time
    print(f"done ({format_elapsed_time(elapsed)})")
    return drives


def print_detected_drives(drives: list[dict[str, str]]) -> None:
    """Print detected optical drives."""
    print("\nDetected optical drives:")
    print("-" * 80)
    for idx, drive in enumerate(drives, 1):
        print(f"[{idx}] Path         : {drive['path']}")
        print(f"    Model        : {drive['model']}")
        print(f"    Media Loaded : {drive['media_loaded']}")
        print(f"    Burn Capable : {drive['burn_capable']}")
        print(f"    Free Space   : {drive['free_space']}")
        print("-" * 80)


def select_burn_drive(drives: list[dict[str, str]]) -> dict[str, str] | None:
    """Prompt user to choose a drive or cancel."""
    while True:
        choice = input("Select drive number (or X to cancel): ").strip().upper()
        if choice == "X":
            return None
        if choice.isdigit():
            drive_index = int(choice)
            if 1 <= drive_index <= len(drives):
                drive = drives[drive_index - 1]
                if int(drive.get("free_space_bytes", "-1")) == 0:
                    print(f"Drive [{drive_index}] has no free space. Insert a blank disc or choose another drive.")
                    continue
                return drive
        print("Invalid selection. Please try again.")


def list_burn_files(folder_path: Path, content_folder: Path, burn_mode: str) -> list[Path]:
    """Return files to burn: README, checksums, and all content files."""
    burn_files: list[Path] = []
    readme_file = folder_path / "README.txt"
    checksums_file = folder_path / "checksums.sha256"

    if readme_file.exists():
        burn_files.append(readme_file)
    if checksums_file.exists():
        burn_files.append(checksums_file)

    burn_files.extend(get_selected_payload_files(content_folder, burn_mode))
    return burn_files


def get_disc_relative_path(filepath: Path, folder_path: Path, content_folder: Path) -> Path:
    """Return the path a file will have at disc root."""
    if filepath.parent == folder_path:
        return filepath.name
    return filepath.relative_to(content_folder)


def print_burn_file_review(folder_path: Path, content_folder: Path, burn_mode: str) -> None:
    """Print the full burn set including metadata files and content files."""
    if burn_mode == AUDIO_DISC_MODE:
        audio_files = get_selected_payload_files(content_folder, burn_mode)
        print("\nAudio tracks prepared for burn:")
        print("-" * 80)
        if not audio_files:
            print("No supported audio files available to burn.")
            print("-" * 80)
            return

        total_bytes = 0
        for idx, filepath in enumerate(audio_files, 1):
            rel_path = filepath.relative_to(content_folder)
            size_bytes = filepath.stat().st_size
            total_bytes += size_bytes
            print(f"[{idx}] {rel_path} ({format_bytes(size_bytes)})")

        print("-" * 80)
        print(f"Total tracks: {len(audio_files)}")
        print(f"Source size : {format_bytes(total_bytes)}")
        print("Note: README.txt and checksums.sha256 remain in the record folder only.")
        return

    burn_files = list_burn_files(folder_path, content_folder, burn_mode)

    print("\nFiles prepared for burn:")
    print("-" * 80)
    if not burn_files:
        print("No files available to burn.")
        print("-" * 80)
        return

    total_bytes = 0
    for idx, filepath in enumerate(burn_files, 1):
        rel_path = get_disc_relative_path(filepath, folder_path, content_folder)
        size_bytes = filepath.stat().st_size
        total_bytes += size_bytes
        print(f"[{idx}] {rel_path} ({format_bytes(size_bytes)})")

    print("-" * 80)
    print(f"Total files: {len(burn_files)}")
    print(f"Total size : {format_bytes(total_bytes)}")


def stage_disc_layout(folder_path: Path, content_folder: Path, staging_dir: Path, burn_mode: str) -> None:
    """Build the exact disc-root layout in a temporary staging directory."""
    readme_file = folder_path / "README.txt"
    checksums_file = folder_path / "checksums.sha256"

    # Collect all files to stage
    files_to_stage = []
    total_bytes = 0
    
    if readme_file.exists():
        files_to_stage.append(("README.txt", readme_file))
        total_bytes += readme_file.stat().st_size
    
    if checksums_file.exists():
        files_to_stage.append(("checksums.sha256", checksums_file))
        total_bytes += checksums_file.stat().st_size

    content_files = get_selected_payload_files(content_folder, burn_mode)
    for source_path in content_files:
        relative_path = source_path.relative_to(content_folder)
        files_to_stage.append((str(relative_path), source_path))
        total_bytes += source_path.stat().st_size

    print(f"Staging disc layout ({len(files_to_stage)} files, {format_bytes(total_bytes)})...")
    
    bytes_copied = 0
    for idx, (display_name, source_path) in enumerate(files_to_stage, 1):
        if display_name in ("README.txt", "checksums.sha256"):
            target_path = staging_dir / display_name
        else:
            target_path = staging_dir / display_name
        
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        
        bytes_copied += source_path.stat().st_size
        percent = (bytes_copied / total_bytes * 100) if total_bytes > 0 else 0
        print(f"  [{idx}/{len(files_to_stage)}] {display_name:<50} {percent:5.1f}%", end="\r")
        sys.stdout.flush()
    
    if files_to_stage:
        print(" " * 80, end="\r")
        sys.stdout.flush()
    
    print(f"[OK] Staged {len(files_to_stage)} files to temporary location")


def burn_disc_windows(selected_drive: dict[str, str], staging_dir: Path, volume_name: str) -> None:
    """Burn a staged directory to optical media on Windows using IMAPI."""
    drive_path = selected_drive["path"]
    if drive_path == "Unavailable":
        raise RuntimeError("Selected drive does not expose a usable drive path.")

    escaped_drive = drive_path.rstrip("\\") + "\\"
    source_path = str(staging_dir.resolve()).replace("'", "''")
    volume_name = sanitize_volume_label(volume_name).replace("'", "''")
    escaped_drive = escaped_drive.replace("'", "''")

    powershell_script = f"""
$ErrorActionPreference = 'Stop'
$sourcePath = '{source_path}'
$drivePath = '{escaped_drive}'
$volumeName = '{volume_name}'
$master = New-Object -ComObject IMAPI2.MsftDiscMaster2
$recorder = $null
foreach ($id in @($master)) {{
    $candidate = New-Object -ComObject IMAPI2.MsftDiscRecorder2
    $candidate.InitializeDiscRecorder($id)
    if ($candidate.VolumePathNames -contains $drivePath) {{
        $recorder = $candidate
        break
    }}
}}
if ($null -eq $recorder) {{
    throw 'No matching recorder found for selected drive.'
}}
$format = New-Object -ComObject IMAPI2.MsftDiscFormat2Data
$format.ClientName = 'burnaboi'
$format.Recorder = $recorder
$format.ForceMediaToBeClosed = $true
$image = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
$image.ChooseImageDefaults($recorder)
$image.FileSystemsToCreate = 4
$image.VolumeName = $volumeName
$image.Root.AddTree($sourcePath, $false)
$result = $image.CreateResultImage()
$format.Write($result.ImageStream)
"""

    code, stdout, stderr = run_command([
        "powershell",
        "-NoProfile",
        "-Command",
        powershell_script,
    ])
    if code != 0:
        detail = stderr.strip() or stdout.strip() or "Unknown IMAPI error"
        raise RuntimeError(detail)


def burn_audio_cd_windows(selected_drive: dict[str, str], prepared_tracks: list[dict[str, str | float | Path]]) -> None:
    """Burn Red Book audio tracks to CD media on Windows using IMAPI track-at-once."""
    drive_path = selected_drive["path"]
    if drive_path == "Unavailable":
        raise RuntimeError("Selected drive does not expose a usable drive path.")
    if not prepared_tracks:
        raise RuntimeError("No audio tracks are available to burn.")

    escaped_drive = drive_path.rstrip("\\") + "\\"
    escaped_drive = escaped_drive.replace("'", "''")
    track_paths_list = []
    for track_info in prepared_tracks:
        raw_path = str(Path(track_info["raw_path"]).resolve())
        track_paths_list.append(raw_path)

    # Pass track paths as a JSON array to PowerShell to avoid quoting/escaping issues
    track_paths_json = json.dumps(track_paths_list)
    powershell_script = f"""
$ErrorActionPreference = 'Stop'
$drivePath = '{escaped_drive}'
$trackPaths = ConvertFrom-Json '{track_paths_json}'
$master = New-Object -ComObject IMAPI2.MsftDiscMaster2
$recorder = $null
foreach ($id in @($master)) {{
    $candidate = New-Object -ComObject IMAPI2.MsftDiscRecorder2
    $candidate.InitializeDiscRecorder($id)
    if ($candidate.VolumePathNames -contains $drivePath) {{
        $recorder = $candidate
        break
    }}
}}
if ($null -eq $recorder) {{
    throw 'No matching recorder found for selected drive.'
}}
$format = New-Object -ComObject IMAPI2.MsftDiscFormat2TrackAtOnce
$format.ClientName = 'burnaboi'
$format.Recorder = $recorder
$format.PrepareMedia()
$format.DoNotFinalizeMedia = $false
foreach ($trackPath in $trackPaths) {{
    $stream = New-Object -ComObject ADODB.Stream
    $stream.Type = 1
    $stream.Open()
    $stream.LoadFromFile($trackPath)
    if ($stream.Size -le 0) {{
        throw "Track stream is empty: $trackPath"
    }}
    $stream.Position = 0
    $format.AddAudioTrack($stream)
    $stream.Close()
}}
$format.ReleaseMedia()
"""

    code, stdout, stderr = run_command([
        "powershell",
        "-NoProfile",
        "-Command",
        powershell_script,
    ])
    if code != 0:
        detail = stderr.strip() or stdout.strip() or "Unknown IMAPI audio burn error"
        raise RuntimeError(detail)


def execute_burn(
    selected_drive: dict[str, str],
    folder_path: Path,
    content_folder: Path,
    volume_name: str,
    burn_mode: str,
) -> None:
    """Perform the actual burn using the current platform's supported toolchain."""
    system_name = platform.system()

    if burn_mode == AUDIO_DISC_MODE:
        if system_name != "Windows":
            raise RuntimeError("True Red Book audio CD burning is currently implemented only on Windows.")

        with tempfile.TemporaryDirectory(prefix="audio-cd-") as temp_dir:
            prepared_dir = Path(temp_dir)
            burn_start = time.time()
            prepared_tracks = prepare_red_book_tracks(content_folder, prepared_dir)
            if not prepared_tracks:
                raise RuntimeError("No supported audio files are available to burn.")

            total_duration = sum(float(track["duration_seconds"]) for track in prepared_tracks)
            print(f"[1/3] Prepared {len(prepared_tracks)} track(s), total duration {format_audio_duration(total_duration)}")
            print("\n[2/3] Sending audio tracks to burner...")
            burn_audio_cd_windows(selected_drive, prepared_tracks)
            print("\n[3/3] Finalizing audio CD...")
            total_elapsed = time.time() - burn_start
            print(f"[OK] Audio CD burn completed in {format_elapsed_time(total_elapsed)}")
            return

    with tempfile.TemporaryDirectory(prefix="disc-burn-") as temp_dir:
        staging_dir = Path(temp_dir)
        
        burn_start = time.time()
        
        # Step 1: Stage files
        stage_disc_layout(folder_path, content_folder, staging_dir, burn_mode)
        stage_elapsed = time.time() - burn_start
        print(f"    Staging time: {format_elapsed_time(stage_elapsed)}")

        if system_name == "Windows":
            # Step 2: Send image to burner
            print("\n[2/3] Sending image to burner...")
            burn_start_time = time.time()
            burn_disc_windows(selected_drive, staging_dir, volume_name)
            burn_elapsed = time.time() - burn_start_time
            print(f"    Burn time: {format_elapsed_time(burn_elapsed)}")
            
            # Step 3: Finalize
            print("\n[3/3] Finalizing disc...")
            total_elapsed = time.time() - burn_start
            print(f"[OK] Burn completed in {format_elapsed_time(total_elapsed)}")
            return

        raise RuntimeError(
            f"Actual burning is not implemented for {system_name}. Drive discovery is available, but writing is currently Windows-only."
        )


def start_burn_journey(
    folder_path: Path,
    content_folder: Path,
    disc_label: str,
    burn_mode: str,
    burned_by: str,
    burn_date: str,
) -> bool:
    """Guided burn workflow: select drive, review files, confirm or refresh."""
    print("\n" + "=" * 80)
    print("Optical Media Burn")
    print("=" * 80)
    
    drives = detect_optical_drives()
    if not drives:
        print("No physical optical drives detected.")
        return False

    print(f"\nFound {len(drives)} optical drive(s):\n")
    print_detected_drives(drives)
    selected_drive = select_burn_drive(drives)
    if selected_drive is None:
        print("Burn journey cancelled.")
        return False

    print(f"\n✓ Selected drive: {selected_drive['path']} ({selected_drive['model']})")
    print(f"[OK] Media loaded: {selected_drive['media_loaded']}")
    print(f"[OK] Burn capable: {selected_drive['burn_capable']}")
    # Allow user to set audio disc length (minutes) for fit checks; default 80 minutes
    audio_max_seconds = RED_BOOK_MAX_SECONDS
    audio_capacity_mb = int(round((RED_BOOK_MAX_SECONDS / 60) * 8.75))  # default 80min->~700MB
    if burn_mode == AUDIO_DISC_MODE:
        try:
            user_input = input(f"Specify max disc length in minutes [{int(RED_BOOK_MAX_SECONDS/60)}]: ").strip()
            if user_input:
                minutes = float(user_input)
                if minutes > 0:
                    audio_max_seconds = int(minutes * 60)
                    audio_capacity_mb = int(round(minutes * 8.75))
        except Exception:
            # on invalid input, keep defaults
            audio_max_seconds = RED_BOOK_MAX_SECONDS
            audio_capacity_mb = int(round((RED_BOOK_MAX_SECONDS / 60) * 8.75))

    iteration = 0
    while True:
        iteration += 1
        print(f"\n--- Burn Review (Iteration {iteration}) ---")
        print("Refreshing burn set...")
        refresh_readme(folder_path, disc_label, burned_by, burn_date, burn_mode, content_folder)
        compute_checksums(content_folder, folder_path, burn_mode)
        print_burn_file_review(folder_path, content_folder, burn_mode)

        # Additional burn-review diagnostics
        selected_files = get_selected_payload_files(content_folder, burn_mode)
        payload_bytes = sum(p.stat().st_size for p in selected_files)
        readme_path = folder_path / "README.txt"
        checksums_path = folder_path / "checksums.sha256"
        readme_bytes = readme_path.stat().st_size if readme_path.exists() else 0
        checksums_bytes = checksums_path.stat().st_size if checksums_path.exists() else 0
        staged_bytes = payload_bytes + readme_bytes + checksums_bytes

        if payload_bytes == staged_bytes:
            print(f"\n[REVIEW] Source payload size equals staged size: {format_bytes(payload_bytes)}")
        else:
            diff = staged_bytes - payload_bytes
            print(f"\n[REVIEW] Source payload: {format_bytes(payload_bytes)}; Staged total (including README/checksums): {format_bytes(staged_bytes)} ({'+' if diff>0 else '-'}{format_bytes(abs(diff))} metadata)")

        if burn_mode == AUDIO_DISC_MODE:
            ffmpeg_path = get_ffmpeg_path()
            ffprobe_path = shutil.which("ffprobe")
            total_seconds = 0.0
            unknown_durations = False
            for p in selected_files:
                d = get_media_duration_seconds(p, ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path)
                if d < 0:
                    # fallback estimate from raw-size (best effort)
                    est = p.stat().st_size / float(RED_BOOK_BYTES_PER_SECOND)
                    total_seconds += est
                    unknown_durations = True
                else:
                    total_seconds += d

            n_tracks = len(selected_files)
            # Reserve 2s lead-in + 2s gaps between tracks (Red Book uses 2s pregap by default)
            reserved_seconds = 2 + (2 * max(0, n_tracks - 1)) if n_tracks > 0 else 0
            total_required_seconds = total_seconds + reserved_seconds
            will_fit = total_required_seconds <= audio_max_seconds

            print("\n[AUDIO ESTIMATE]")
            print(f" - Estimated audio duration: {format_audio_duration(total_seconds)} ({total_seconds:.1f}s){' (some files estimated)' if unknown_durations else ''}")
            print(f" - Reserved for lead-in/gaps: {format_audio_duration(reserved_seconds)} ({reserved_seconds:.1f}s)")
            print(f" - Total required (incl. reserved): {format_audio_duration(total_required_seconds)} of {format_audio_duration(audio_max_seconds)}")
            print(f" - Disc capacity used (approx): {audio_capacity_mb} MB")
            print(f" - Will fit on {int(audio_max_seconds/60)}-minute Red Book CD: {'Yes' if will_fit else 'No'}")

        print("\nBurn review options:")
        print("(C)  Confirm burn set")
        print("(R)  Recheck/refresh")
        print("(X)  Cancel burn journey")

        choice = input("\nEnter option: ").strip().upper()
        if choice == "C":
            print("\n" + "-" * 80)
            print("BURN CONFIRMATION - Final verification")
            print("-" * 80)
            print("Final checksum evaluation before burn...")
            refresh_readme(folder_path, disc_label, burned_by, burn_date, burn_mode, content_folder)
            compute_checksums(content_folder, folder_path, burn_mode)
            volume_name = sanitize_volume_label(disc_label)

            media_type_name = ""
            if burn_mode == AUDIO_DISC_MODE:
                try:
                    _, media_type_name = ensure_audio_cd_media_is_writable(selected_drive)
                except RuntimeError as error:
                    print(f"\n[ERROR] {error}", file=sys.stderr)
                    print("Insert blank CD-R or CD-RW media and choose confirm again.")
                    continue

            print("\n[STATUS] All checks passed. Proceeding with burn...")
            print(f"[DRIVE]  {selected_drive['path']} ({selected_drive['model']})")
            print(f"[LABEL]  {disc_label}")
            if burn_mode == AUDIO_DISC_MODE:
                print("[DISC]   True Red Book Audio CD")
                print(f"[MEDIA]  {media_type_name}")
                print("[NOTE]   README.txt and checksums.sha256 will stay in the record folder and will not be written to the disc.")
            else:
                print(f"[VOLUME] {volume_name}")
            print(f"[MODE]   {format_burn_mode_label(burn_mode)}")
            
            confirm = input("\nReady to burn? Confirm with 'yes' to proceed: ").strip().lower()
            if confirm != "yes":
                print("Burn cancelled by user.")
                continue
            
            print("\n" + "=" * 80)
            print("BURN IN PROGRESS")
            print("=" * 80)
            print("[1/3] Preparing disc layout...")
            try:
                execute_burn(selected_drive, folder_path, content_folder, disc_label, burn_mode)
                removed_files, removed_dirs = clear_content_folder(content_folder)
                print("\n" + "=" * 80)
                print("BURN SUCCESSFUL")
                print("=" * 80)
                print(f"Your optical disc has been created successfully.")
                print(f"Content cleanup completed: {removed_files} files and {removed_dirs} folders removed.")
                if burn_mode == AUDIO_DISC_MODE:
                    print("Disc type: Red Book Audio CD")
                else:
                    print(f"Disc label: {disc_label}")
                print(f"Date: {date.today().strftime('%Y-%m-%d')}")
                return True
            except RuntimeError as error:
                print(f"\n[ERROR] Burn failed: {error}", file=sys.stderr)
                print("Returning to burn review. You can refresh and try again.")
                continue
        elif choice == "R":
            print("Rechecking files and checksums...")
            continue
        elif choice == "X":
            print("Burn journey cancelled.")
            return False
        else:
            print("Invalid option. Please try again.")


def compute_checksums(content_folder: Path, folder_path: Path, burn_mode: str) -> None:
    """Compute SHA256 checksums for all files in content folder and combined total."""
    files = get_selected_payload_files(content_folder, burn_mode)
    
    if not files:
        if burn_mode == AUDIO_DISC_MODE:
            print("\nNo supported audio files found in content folder. Generating empty checksums file...")
        else:
            print(f"\nNo files found in content folder. Generating empty checksums file...")
        total_checksum = hashlib.sha256().hexdigest()
        checksums_file = folder_path / "checksums.sha256"
        checksums_content = "\n# Total combined checksum:\n"
        checksums_content += f"{total_checksum}  (total)\n"
        write_text_file(checksums_file, checksums_content)
        print(f"[OK] Empty checksums file created\n")
        return
    
    start_time = time.time()
    print(
        f"\nComputing SHA256 checksums for {format_burn_mode_label(burn_mode)} "
        f"({len(files)} file{'s' if len(files) != 1 else ''})..."
    )
    
    checksums = []
    combined_hash = hashlib.sha256()
    total_bytes = 0

    for i, filepath in enumerate(files, 1):
        file_size = filepath.stat().st_size
        total_bytes += file_size
        print(f"  [{i}/{len(files)}] {filepath.name:<50} {format_bytes(file_size):<12}", end="\r")
        sys.stdout.flush()
        
        checksum = compute_sha256_file(filepath)
        rel_path = filepath.relative_to(content_folder)
        checksums.append(f"{checksum}  {rel_path}")
        
        # Add file content to combined hash
        with open(filepath, "rb") as f:
            combined_hash.update(f.read())
    
    print(" " * 80, end="\r")  # Clear the line
    sys.stdout.flush()
    
    elapsed = time.time() - start_time
    total_checksum = combined_hash.hexdigest()
    
    checksums_file = folder_path / "checksums.sha256"
    checksums_content = "\n".join(checksums) + "\n"
    checksums_content += "\n# Total combined checksum:\n"
    checksums_content += f"{total_checksum}  (total)\n"
    write_text_file(checksums_file, checksums_content)

    print(f"[OK] Checksums computed and saved to: {checksums_file}")
    print(f"     Total: {len(files)} files, {format_bytes(total_bytes)} in {format_elapsed_time(elapsed)}\n")
    
    for checksum_line in checksums:
        print(checksum_line)
    print(f"\n--- Combined Total ---")
    print(f"{total_checksum}  (total)")


def show_options_menu(folder_path: Path, disc_label: str, burn_mode: str, burned_by: str, burn_date: str) -> None:
    """Show post-setup options menu."""
    content_folder = folder_path / "content"
    
    while True:
        print("\n--- Options ---")
        print("(B)  Start Burn Journey")
        print("(C)  Compute SHA256 checksums")
        print("(Q)  Quit")
        
        choice = input("\nEnter option: ").strip().upper()
        
        if choice == "B":
            if start_burn_journey(folder_path, content_folder, disc_label, burn_mode, burned_by, burn_date):
                print("Done.")
                break
        elif choice == "C":
            refresh_readme(folder_path, disc_label, burned_by, burn_date, burn_mode, content_folder)
            compute_checksums(content_folder, folder_path, burn_mode)
        elif choice == "Q":
            print("Done.")
            break
        else:
            print("Invalid option. Please try again.")


def main():
    print("=" * 80)
    print("OPTICAL DISC ARCHIVE - New Burn Setup")
    print("=" * 80)
    print()
    burn_mode = prompt_burn_mode()
    disc_label = prompt_disc_label()
    burned_by = prompt_burned_by()
    burn_date = date.today().strftime("%Y-%m-%d")
    folder_name = f"{date.today().strftime('%Y%m%d')}-{sanitize_label_for_folder(disc_label)}"

    print(f"\n[OK] Burn Mode  : {format_burn_mode_label(burn_mode)}")
    print(f"\n[OK] Disc Label : {disc_label}")
    print(f"[OK] Burned By  : {burned_by}")
    print(f"[OK] Date       : {burn_date}")
    print(f"[OK] Folder     : records/{folder_name}/")

    print(f"\nCreating folder structure...")
    records_dir = Path("records")
    folder_path = create_folders(records_dir, folder_name)
    print(f"[OK] Main folder created: {folder_path}")
    print(f"[OK] Content folder ready: {folder_path / 'content'}")

    print(f"\nGenerating README from template...")
    readme_path = populate_and_write_readme(
        folder_path,
        disc_label,
        burned_by,
        burn_date,
        burn_mode,
        folder_path / "content",
    )
    print(f"[OK] README created: {readme_path}")

    print(f"\nFolder is ready for files: {folder_path / 'content'}")
    print("You can now add files to the content folder.")
    if burn_mode == AUDIO_DISC_MODE:
        print("Only common player-friendly audio formats will be included in checksums, README summary, and burn staging.")
    print()
    
    show_options_menu(folder_path, disc_label, burn_mode, burned_by, burn_date)


if __name__ == "__main__":
    main()
