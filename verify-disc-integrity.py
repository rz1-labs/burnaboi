#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path


TEXT_ENCODING = "utf-8"
IGNORED_DISC_FILES = {"checksums.sha256", "readme.txt"}
MANIFEST_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.*)$")
DISC_LABEL_RE = re.compile(r"^Disc Label:\s*(.+?)\s*$", re.MULTILINE)
DATE_BURNED_RE = re.compile(r"^Date Burned:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
LAST_REVERIFIED_RE = re.compile(r"^(Last Re-Verified:\s*)(.*)$", re.MULTILINE)


@dataclass(frozen=True)
class DriveOption:
	path: Path
	label: str
	details: str
	media_loaded: bool = False
	write_status: str = "Unknown"


@dataclass(frozen=True)
class ManifestEntry:
	relative_path: Path
	expected_hash: str


@dataclass(frozen=True)
class DiscMetadata:
	disc_label: str
	date_burned: str
	readme_path: Path


@dataclass(frozen=True)
class RecordMatch:
	record_folder: Path
	readme_path: Path
	disc_label: str
	date_burned: str


def configure_stdio() -> None:
	for stream_name in ("stdout", "stderr"):
		stream = getattr(sys, stream_name, None)
		if stream is None or not hasattr(stream, "reconfigure"):
			continue
		try:
			stream.reconfigure(encoding=TEXT_ENCODING, errors="backslashreplace")
		except (ValueError, OSError):
			continue


def format_bytes(num_bytes: int) -> str:
	units = ["B", "KB", "MB", "GB", "TB"]
	value = float(num_bytes)
	for unit in units:
		if value < 1024.0 or unit == units[-1]:
			return f"{value:.1f} {unit}"
		value /= 1024.0
	return f"{num_bytes} B"


def run_command(command: list[str]) -> tuple[int, str, str]:
	result = subprocess.run(command, capture_output=True, text=True, check=False)
	return result.returncode, result.stdout, result.stderr


def is_likely_virtual_drive(text: str) -> bool:
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
	lowered = text.lower()
	return any(keyword in lowered for keyword in virtual_keywords)


def get_disk_usage_text(path: Path) -> str:
	try:
		usage = os.statvfs(path) if hasattr(os, "statvfs") else None
		if usage is not None:
			free_bytes = usage.f_bavail * usage.f_frsize
			return format_bytes(free_bytes)
	except OSError:
		pass

	try:
		import shutil

		return format_bytes(shutil.disk_usage(path).free)
	except OSError:
		return "Unavailable"


def get_disk_free_bytes(path: Path) -> int:
	try:
		import shutil

		return shutil.disk_usage(path).free
	except OSError:
		return -1


def read_windows_volume_label(root: str) -> str:
	volume_name = ctypes.create_unicode_buffer(261)
	file_system_name = ctypes.create_unicode_buffer(261)
	serial_number = ctypes.c_ulong()
	max_component_len = ctypes.c_ulong()
	file_system_flags = ctypes.c_ulong()

	get_volume_information = ctypes.windll.kernel32.GetVolumeInformationW
	ok = get_volume_information(
		ctypes.c_wchar_p(root),
		volume_name,
		ctypes.sizeof(volume_name),
		ctypes.byref(serial_number),
		ctypes.byref(max_component_len),
		ctypes.byref(file_system_flags),
		file_system_name,
		ctypes.sizeof(file_system_name),
	)
	if not ok:
		return ""
	return volume_name.value.strip()


def discover_drives_windows() -> list[DriveOption]:
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

	drives: list[DriveOption] = []
	for item in raw:
		drive_letter = str(item.get("Drive") or "").strip()
		caption = str(item.get("Caption") or "Unknown").strip()
		manufacturer = str(item.get("Manufacturer") or "").strip()
		combined_text = f"{caption} {manufacturer}".strip()
		if is_likely_virtual_drive(combined_text):
			continue

		root = f"{drive_letter}\\" if drive_letter else ""
		root_path = Path(root) if root else Path(".")
		label = read_windows_volume_label(root) if root else ""
		media_loaded = bool(item.get("MediaLoaded"))
		caps = str(item.get("CapabilityDescriptions") or "")
		caps_lower = caps.lower()
		drive_can_write = any(keyword in caps_lower for keyword in ("write", "writing", "record", "burn", "dvd-r", "bd-r"))
		free_bytes = get_disk_free_bytes(root_path) if root and media_loaded else -1
		path_writable = os.access(root_path, os.W_OK) if root and media_loaded else False
		if not media_loaded:
			write_status = "No media loaded"
		elif free_bytes == 0:
			write_status = "Readable only"
		elif drive_can_write and free_bytes > 0:
			write_status = "Writable"
		elif drive_can_write and path_writable and free_bytes < 0:
			write_status = "Writable"
		elif drive_can_write:
			write_status = "Readable only"
		else:
			write_status = "Readable only"

		details_parts = ["Physical optical drive", caption]
		if manufacturer:
			details_parts.append(manufacturer)
		details_parts.append(f"Media loaded: {'Yes' if media_loaded else 'No'}")
		details_parts.append(f"Write status: {write_status}")
		if root:
			details_parts.append(f"Path: {root}")
		if label:
			details_parts.append(f"Label: {label}")
		if root and media_loaded:
			details_parts.append(f"Free: {get_disk_usage_text(root_path)}")

		display_label = root or caption
		drives.append(
			DriveOption(
				path=root_path if root else Path("."),
				label=display_label,
				details=" | ".join(part for part in details_parts if part),
				media_loaded=media_loaded,
				write_status=write_status,
			)
		)

	drives.sort(key=lambda item: str(item.path).lower())
	return drives


def discover_drives_linux() -> list[DriveOption]:
	block_root = Path("/sys/class/block")
	if not block_root.exists():
		return []

	drives: list[DriveOption] = []
	for entry in sorted(block_root.iterdir()):
		dev_type = entry / "device" / "type"
		if not dev_type.exists():
			continue

		try:
			if dev_type.read_text(encoding=TEXT_ENCODING).strip() != "5":
				continue
		except OSError:
			continue

		model_path = entry / "device" / "model"
		vendor_path = entry / "device" / "vendor"
		model = " ".join(
			value.strip()
			for value in [
				vendor_path.read_text(encoding=TEXT_ENCODING).strip() if vendor_path.exists() else "",
				model_path.read_text(encoding=TEXT_ENCODING).strip() if model_path.exists() else "",
			]
			if value.strip()
		)
		if model and is_likely_virtual_drive(model):
			continue

		device_path = f"/dev/{entry.name}"
		mount_path = ""
		filesystem = ""
		try:
			with open("/proc/mounts", "r", encoding=TEXT_ENCODING) as mounts_file:
				for line in mounts_file:
					parts = line.split()
					if len(parts) < 3:
						continue
					if parts[0] == device_path:
						mount_path = parts[1]
						filesystem = parts[2]
						break
		except OSError:
			mount_path = ""

		target_path = Path(mount_path) if mount_path else Path(device_path)
		details_parts = ["Physical optical drive", model or entry.name]
		details_parts.append(f"Device: {device_path}")
		details_parts.append(f"Media loaded: {'Yes' if mount_path else 'No'}")
		write_status = "Readable only" if mount_path else "No media loaded"
		details_parts.append(f"Write status: {write_status}")
		if filesystem:
			details_parts.append(f"Filesystem: {filesystem}")
		if mount_path:
			details_parts.append(f"Mount: {mount_path}")
			details_parts.append(f"Free: {get_disk_usage_text(Path(mount_path))}")

		drives.append(
			DriveOption(
				path=target_path,
				label=mount_path or device_path,
				details=" | ".join(part for part in details_parts if part),
				media_loaded=bool(mount_path),
				write_status=write_status,
			)
		)

	drives.sort(key=lambda item: str(item.path).lower())
	return drives


def discover_drives_macos() -> list[DriveOption]:
	code, stdout, _ = run_command(["drutil", "status"])
	if code != 0:
		return []

	if "No drives found" in stdout:
		return []

	drives: list[DriveOption] = []
	model_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
	model = "Unknown"
	for line in model_lines:
		lowered = line.lower()
		if lowered.startswith("vendor") or lowered.startswith("product"):
			model = line
			break

	mount_root = Path("/Volumes")
	mounted_volumes = sorted(path for path in mount_root.iterdir() if path.is_dir()) if mount_root.exists() else []
	if mounted_volumes:
		for mount_path in mounted_volumes:
			write_status = "Readable only"
			details = f"Physical optical drive | {model} | Write status: {write_status} | Mount: {mount_path} | Free: {get_disk_usage_text(mount_path)}"
			drives.append(
				DriveOption(
					path=mount_path,
					label=str(mount_path),
					details=details,
					media_loaded=True,
					write_status=write_status,
				)
			)
	else:
		drives.append(
			DriveOption(
				path=Path("/Volumes"),
				label="Optical Drive",
				details=f"Physical optical drive | {model} | Media loaded: Unknown",
				media_loaded=False,
				write_status="Unknown",
			)
		)

	drives.sort(key=lambda item: str(item.path).lower())
	return drives


def discover_drives() -> list[DriveOption]:
	system_name = platform.system()
	if system_name == "Windows":
		return discover_drives_windows()
	if system_name == "Linux":
		return discover_drives_linux()
	if system_name == "Darwin":
		return discover_drives_macos()
	return []


def print_drive_options(drives: list[DriveOption]) -> None:
	print("Detected physical optical drives:")
	print("-" * 80)
	for index, drive in enumerate(drives, start=1):
		print(f"[{index}] {drive.label}")
		print(f"    {drive.details}")
	print("[R] Refresh drive list")
	print("[M] Enter a path manually")
	print("[X] Cancel")
	print("-" * 80)


def prompt_for_drive(drives: list[DriveOption]) -> DriveOption | Path | str | None:
	while True:
		choice = input("Select an optical drive: ").strip()
		if not choice:
			print("Please enter a selection.")
			continue

		upper_choice = choice.upper()
		if upper_choice == "X":
			return None
		if upper_choice == "R":
			return "refresh"
		if upper_choice == "M":
			manual_path = Path(input("Enter disc path: ").strip()).expanduser()
			if manual_path.exists() and manual_path.is_dir():
				return manual_path.resolve()
			print("That path is not a readable directory.")
			continue
		if choice.isdigit():
			index = int(choice)
			if 1 <= index <= len(drives):
				return drives[index - 1]

		print("Invalid selection. Try again.")


def print_drive_notice(drive: DriveOption) -> None:
	if drive.media_loaded and drive.write_status == "Readable only":
		print("Notice: selected disc appears readable but not writable. Verification will continue in read-only mode.")
	elif drive.media_loaded and drive.write_status == "Writable":
		print("Notice: selected disc appears writable. This tool only reads and verifies; it will not write anything.")


def normalize_manifest_path(raw_value: str) -> Path:
	return Path(raw_value.replace("\\", "/"))


def sanitize_disc_label(label: str) -> str:
	value = label.upper().replace(" ", "-")
	value = re.sub(r"[^A-Z0-9\-_]", "", value)
	value = re.sub(r"-{2,}", "-", value)
	return value.strip("-_")


def find_checksums_file(disc_root: Path) -> Path | None:
	direct_match = disc_root / "checksums.sha256"
	if direct_match.exists() and direct_match.is_file():
		return direct_match

	matches = sorted(
		path for path in disc_root.rglob("*") if path.is_file() and path.name.lower() == "checksums.sha256"
	)
	if not matches:
		return None
	return matches[0]


def find_readme_file(disc_root: Path) -> Path | None:
	direct_match = disc_root / "README.txt"
	if direct_match.exists() and direct_match.is_file():
		return direct_match

	matches = sorted(
		path for path in disc_root.rglob("*") if path.is_file() and path.name.lower() == "readme.txt"
	)
	if not matches:
		return None
	return matches[0]


def parse_disc_metadata(readme_path: Path) -> DiscMetadata | None:
	content = readme_path.read_text(encoding=TEXT_ENCODING)
	disc_label_match = DISC_LABEL_RE.search(content)
	date_burned_match = DATE_BURNED_RE.search(content)
	if disc_label_match is None or date_burned_match is None:
		return None

	return DiscMetadata(
		disc_label=disc_label_match.group(1).strip(),
		date_burned=date_burned_match.group(1).strip(),
		readme_path=readme_path,
	)


def find_matching_record(metadata: DiscMetadata) -> RecordMatch | None:
	repo_root = Path(__file__).resolve().parent
	records_root = repo_root / "records"
	if not records_root.exists() or not records_root.is_dir():
		return None

	expected_folder_name = f"{metadata.date_burned.replace('-', '')}-{sanitize_disc_label(metadata.disc_label)}"
	record_folder = records_root / expected_folder_name
	readme_path = record_folder / "README.txt"
	if not record_folder.exists() or not readme_path.is_file():
		return None

	return RecordMatch(
		record_folder=record_folder,
		readme_path=readme_path,
		disc_label=metadata.disc_label,
		date_burned=metadata.date_burned,
	)


def print_record_match(record_match: RecordMatch) -> None:
	print("\nRecord match:")
	print("-" * 80)
	print(f"Disc Label  : {record_match.disc_label}")
	print(f"Date Burned : {record_match.date_burned}")
	print(f"Record Dir  : {record_match.record_folder}")
	print(f"README      : {record_match.readme_path}")


def update_last_reverified(readme_path: Path, passed: bool) -> bool:
	content = readme_path.read_text(encoding=TEXT_ENCODING)
	status_text = "Pass" if passed else "Fail"
	replacement = f"\\g<1>{date.today().isoformat()} — {status_text}"
	updated_content, replacements = LAST_REVERIFIED_RE.subn(replacement, content, count=1)
	if replacements != 1:
		return False

	readme_path.write_text(updated_content, encoding=TEXT_ENCODING)
	return True


def prompt_yes_no(message: str) -> bool:
	choice = input(f"{message} [y/N]: ").strip().lower()
	return choice in {"y", "yes"}


def choose_verification_root(checksums_file: Path, manifest_entries: list[ManifestEntry]) -> Path:
	candidates = [checksums_file.parent]
	content_candidate = checksums_file.parent / "content"
	if content_candidate.exists() and content_candidate.is_dir():
		candidates.append(content_candidate)

	best_root = candidates[0]
	best_score = -1
	for candidate in candidates:
		score = 0
		for entry in manifest_entries:
			if (candidate / entry.relative_path).is_file():
				score += 1
		if score > best_score:
			best_root = candidate
			best_score = score

	return best_root


def parse_checksums_manifest(checksums_file: Path) -> tuple[list[ManifestEntry], str | None]:
	manifest_entries: list[ManifestEntry] = []
	total_checksum: str | None = None

	for line_number, raw_line in enumerate(checksums_file.read_text(encoding=TEXT_ENCODING).splitlines(), start=1):
		line = raw_line.strip()
		if not line or line.startswith("#"):
			continue

		match = MANIFEST_LINE_RE.match(line)
		if not match:
			raise ValueError(f"Unsupported checksum line at {checksums_file}:{line_number}: {raw_line}")

		checksum_value = match.group(1).lower()
		path_text = match.group(2).strip()
		if path_text == "(total)":
			total_checksum = checksum_value
			continue

		manifest_entries.append(
			ManifestEntry(relative_path=normalize_manifest_path(path_text), expected_hash=checksum_value)
		)

	return manifest_entries, total_checksum


def compare_checksums_files(disc_checksums_file: Path, record_checksums_file: Path) -> bool:
	if not record_checksums_file.exists() or not record_checksums_file.is_file():
		print(f"Record checksums file missing: {record_checksums_file}")
		return False

	disc_text = disc_checksums_file.read_text(encoding=TEXT_ENCODING).replace("\r\n", "\n")
	record_text = record_checksums_file.read_text(encoding=TEXT_ENCODING).replace("\r\n", "\n")
	if disc_text == record_text:
		print("[OK] Record checksum file matches disc checksums.sha256.")
		return True

	print("[FAIL] Record checksum file does not match disc checksums.sha256.")
	print(f"       Disc  : {disc_checksums_file}")
	print(f"       Record: {record_checksums_file}")
	return False


def compute_sha256_file(path: Path, combined_hash: hashlib._Hash | None = None) -> str:
	file_hash = hashlib.sha256()
	with open(path, "rb") as file_handle:
		for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
			file_hash.update(chunk)
			if combined_hash is not None:
				combined_hash.update(chunk)
	return file_hash.hexdigest()


def should_ignore_for_total(relative_path: Path) -> bool:
	return relative_path.name.lower() in IGNORED_DISC_FILES


def list_disc_content_files(disc_root: Path) -> list[Path]:
	files = []
	for path in disc_root.rglob("*"):
		if not path.is_file():
			continue
		relative_path = path.relative_to(disc_root)
		if should_ignore_for_total(relative_path):
			continue
		files.append(path)
	return sorted(files, key=lambda item: item.relative_to(disc_root).as_posix().lower())


def verify_manifest_entries(disc_root: Path, manifest_entries: list[ManifestEntry]) -> tuple[bool, list[Path]]:
	print("\nPer-file checksum results:")
	print("-" * 80)
	all_ok = True
	manifest_paths: list[Path] = []

	for entry in manifest_entries:
		manifest_paths.append(entry.relative_path)
		target_path = disc_root / entry.relative_path
		display_path = entry.relative_path.as_posix()

		if not target_path.exists() or not target_path.is_file():
			print(f"[MISSING] {display_path}")
			all_ok = False
			continue

		actual_hash = compute_sha256_file(target_path)
		if actual_hash == entry.expected_hash:
			print(f"[OK]      {display_path}")
		else:
			print(f"[FAIL]    {display_path}")
			print(f"          expected: {entry.expected_hash}")
			print(f"          actual:   {actual_hash}")
			all_ok = False

	return all_ok, manifest_paths


def report_manifest_coverage(disc_root: Path, manifest_paths: list[Path]) -> bool:
	expected_set = {path.as_posix() for path in manifest_paths}
	actual_files = list_disc_content_files(disc_root)
	actual_set = {path.relative_to(disc_root).as_posix() for path in actual_files}

	missing_from_manifest = sorted(actual_set - expected_set)
	missing_on_disc = sorted(expected_set - actual_set)

	ok = True
	if missing_from_manifest:
		ok = False
		print("\nFiles present on disc but missing from checksums.sha256:")
		for relative_path in missing_from_manifest:
			print(f"  - {relative_path}")

	if missing_on_disc:
		ok = False
		print("\nFiles listed in checksums.sha256 but not found on disc:")
		for relative_path in missing_on_disc:
			print(f"  - {relative_path}")

	return ok


def verify_total_checksum(disc_root: Path, expected_total_checksum: str | None) -> bool:
	files = list_disc_content_files(disc_root)
	combined_hash = hashlib.sha256()
	total_bytes = 0

	for path in files:
		total_bytes += path.stat().st_size
		compute_sha256_file(path, combined_hash=combined_hash)

	actual_total = combined_hash.hexdigest()
	expected_total = expected_total_checksum or hashlib.sha256().hexdigest()

	print("\nCombined checksum result:")
	print("-" * 80)
	print(f"Files included : {len(files)}")
	print(f"Total size     : {format_bytes(total_bytes)}")
	print(f"Expected total : {expected_total}")
	print(f"Actual total   : {actual_total}")

	if actual_total == expected_total:
		print("[OK] Combined checksum matches.")
		return True

	print("[FAIL] Combined checksum does not match.")
	return False


def validate_disc(disc_root: Path, selected_drive: DriveOption | None = None) -> int:
	if not disc_root.exists() or not disc_root.is_dir():
		print(f"Path is not a readable directory: {disc_root}", file=sys.stderr)
		return 2

	checksums_file = find_checksums_file(disc_root)
	readme_file = find_readme_file(disc_root)
	if checksums_file is None:
		print(f"No checksums.sha256 file found under: {disc_root}", file=sys.stderr)
		return 2
	if readme_file is None:
		print(f"No README.txt file found under: {disc_root}", file=sys.stderr)
		return 2

	manifest_root = checksums_file.parent
	print(f"Using disc root: {manifest_root}")
	print(f"Using checksum file: {checksums_file}")
	print(f"Using disc README: {readme_file}")

	disc_metadata = parse_disc_metadata(readme_file)
	if disc_metadata is None:
		print(f"Could not read Disc Label and Date Burned from: {readme_file}", file=sys.stderr)
		return 2

	record_match = find_matching_record(disc_metadata)
	if record_match is None:
		expected_folder_name = f"{disc_metadata.date_burned.replace('-', '')}-{sanitize_disc_label(disc_metadata.disc_label)}"
		print(f"No matching record folder found under records for: {expected_folder_name}", file=sys.stderr)
		return 2

	print_record_match(record_match)

	try:
		manifest_entries, total_checksum = parse_checksums_manifest(checksums_file)
	except ValueError as error:
		print(str(error), file=sys.stderr)
		return 2

	verification_root = choose_verification_root(checksums_file, manifest_entries)
	print(f"Validating file payload under: {verification_root}")

	per_file_ok, manifest_paths = verify_manifest_entries(verification_root, manifest_entries)
	coverage_ok = report_manifest_coverage(verification_root, manifest_paths)
	total_ok = verify_total_checksum(verification_root, total_checksum)
	record_checksums_file = record_match.record_folder / "checksums.sha256"
	print("\nRecord checksum match:")
	print("-" * 80)
	manifest_match_ok = compare_checksums_files(checksums_file, record_checksums_file)

	print("\nSummary:")
	print("-" * 80)
	print(f"Per-file verification : {'PASS' if per_file_ok else 'FAIL'}")
	print(f"Manifest coverage     : {'PASS' if coverage_ok else 'FAIL'}")
	print(f"Combined checksum     : {'PASS' if total_ok else 'FAIL'}")
	print(f"Record checksum match : {'PASS' if manifest_match_ok else 'FAIL'}")

	verification_passed = per_file_ok and coverage_ok and total_ok and manifest_match_ok
	if update_last_reverified(record_match.readme_path, verification_passed):
		print(f"Updated Last Re-Verified in: {record_match.readme_path}")
	else:
		print(f"Could not update Last Re-Verified in: {record_match.readme_path}", file=sys.stderr)

	can_update_disc_readme = (
		selected_drive is not None
		and selected_drive.write_status == "Writable"
		and readme_file.exists()
		and os.access(readme_file, os.W_OK)
	)
	if can_update_disc_readme:
		try:
			same_file = readme_file.resolve() == record_match.readme_path.resolve()
		except OSError:
			same_file = False
		if not same_file and prompt_yes_no("Disc appears writable. Update Last Re-Verified on disc README too?"):
			if update_last_reverified(readme_file, verification_passed):
				print(f"Updated Last Re-Verified on disc: {readme_file}")
			else:
				print(f"Could not update Last Re-Verified on disc: {readme_file}", file=sys.stderr)

	return 0 if verification_passed else 1


def main() -> int:
	configure_stdio()

	while True:
		drives = discover_drives()
		if not drives:
			print("No physical optical drives were detected automatically.")
		else:
			print_drive_options(drives)

		selected_path = prompt_for_drive(drives)
		if selected_path is None:
			print("Cancelled.")
			return 1
		if selected_path == "refresh":
			print("Refreshing drive list...\n")
			continue
		if isinstance(selected_path, DriveOption):
			print_drive_notice(selected_path)
			return validate_disc(selected_path.path, selected_drive=selected_path)

		return validate_disc(selected_path)


if __name__ == "__main__":
	raise SystemExit(main())
