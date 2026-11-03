"""Bounded archive inventory: names and filesystem metadata, never contents."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from typing import Iterable

from .core import LabError, new_output, write_json


PACKAGE_ROOT = Path(__file__).resolve().parent
ARCHIVE_ROOT = PACKAGE_ROOT / "fixtures"
FIXTURE_ROOT = PACKAGE_ROOT / "fixtures"


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _reject_reparse_components(path: Path) -> None:
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            raise LabError(f"Archive path does not exist: {path}") from None
        except OSError as exc:
            raise LabError(f"Cannot inspect archive path component: {part.name}") from exc
        if _is_reparse(info):
            raise LabError("Archive paths must not traverse symlinks or reparse points")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def inspect_archive(
    root: str | Path,
    out: str | Path | None,
    max_entries: int = 1000,
    max_depth: int = 16,
    *,
    allowed_roots: Iterable[str | Path] | None = None,
) -> Path:
    """Inventory metadata under the authorized archive or package fixtures.

    ``allowed_roots`` is a Python test seam, never exposed by the CLI.
    Entry limits count files and directories. Reaching either limit is
    conservatively reported as incomplete, including an exact entry limit.
    No file bytes are read, hashes calculated, imports made, or scripts run.
    """
    if type(max_entries) is not int or not 1 <= max_entries <= 100000:
        raise LabError("max_entries must be an integer between 1 and 100000")
    if type(max_depth) is not int or not 1 <= max_depth <= 64:
        raise LabError("max_depth must be an integer between 1 and 64")

    if out is None:
        raise LabError("A fresh evidence output directory must be specified")
    requested = Path(root).absolute()
    if ".." in requested.parts:
        raise LabError("Archive path traversal is forbidden")
    permitted = tuple(Path(item).absolute() for item in (
        allowed_roots if allowed_roots is not None else (ARCHIVE_ROOT, FIXTURE_ROOT)
    ))
    if not permitted or not any(_inside(requested, base) for base in permitted):
        raise LabError("Archive root is outside the authorized archive and fixtures")
    _reject_reparse_components(requested)
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise LabError("Cannot resolve archive root") from exc
    if not any(_inside(resolved, base.resolve()) for base in permitted):
        raise LabError("Resolved archive root escapes its authorized root")
    if not resolved.is_dir():
        raise LabError("Archive root must be a directory")

    entries: list[dict[str, object]] = []
    incomplete: set[str] = set()
    stack: list[tuple[Path, int]] = [(resolved, 0)]
    while stack and len(entries) < max_entries:
        directory, parent_depth = stack.pop()
        _reject_reparse_components(directory)
        remaining = max_entries - len(entries)
        children: list[os.DirEntry[str]] = []
        try:
            with os.scandir(directory) as scanner:
                for child in scanner:
                    children.append(child)
                    if len(children) > remaining:
                        incomplete.add("max_entries_reached")
                        break
        except OSError as exc:
            raise LabError(f"Cannot enumerate archive directory: {directory.name}") from exc
        children.sort(key=lambda child: (child.name.casefold(), child.name))
        directories: list[tuple[Path, int]] = []
        for child in children[:remaining]:
            path = Path(child.path)
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise LabError(f"Cannot inspect archive entry: {child.name}") from exc
            if _is_reparse(info):
                raise LabError("Archive contains a symlink or reparse point; traversal refused")
            if not _inside(path.resolve(), resolved):
                raise LabError("Archive entry resolves outside the inventory root")
            if stat.S_ISDIR(info.st_mode):
                kind = "directory"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                kind = "other"
                incomplete.add("unsupported_entry_type")
            depth = parent_depth + 1
            entries.append({
                "path": path.relative_to(resolved).as_posix(),
                "kind": kind,
                "bytes": info.st_size if kind == "file" else None,
                "mtime_utc": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                "extension": path.suffix.lower() if kind == "file" else "",
                "depth": depth,
            })
            if kind == "directory":
                if depth >= max_depth:
                    incomplete.add("max_depth_reached")
                else:
                    directories.append((path, depth))
        stack.extend(reversed(directories))
    if len(entries) >= max_entries:
        incomplete.add("max_entries_reached")
    if stack:
        incomplete.add("unvisited_directories")

    entries.sort(key=lambda entry: str(entry["path"]))
    file_entries = [entry for entry in entries if entry["kind"] == "file"]
    document = {
        "schema": "mission-lab.archive-inventory.v1",
        "root": str(resolved),
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "filesystem_metadata_only",
        "file_contents_read": False,
        "atomic_snapshot": False,
        "limits": {"max_entries": max_entries, "max_depth": max_depth},
        "complete": not incomplete,
        "incomplete_reasons": sorted(incomplete),
        "counts": {
            "entries": len(entries),
            "files": len(file_entries),
            "directories": sum(entry["kind"] == "directory" for entry in entries),
            "observed_file_bytes": sum(int(entry["bytes"]) for entry in file_entries),
            "extensions": dict(sorted(Counter(str(entry["extension"]) for entry in file_entries).items())),
        },
        "limitations": [
            "Presence and byte length do not establish provenance, identity, integrity, or usability.",
            "Concurrent archive changes can affect this non-atomic metadata snapshot.",
            "Truncated inventories do not establish total archive counts or sizes.",
            "No checkpoints, activation arrays, GGUF tensors, or executable files were opened.",
        ],
        "entries": entries,
    }
    destination = new_output(out)
    write_json(destination / "inventory.json", document)
    return destination
