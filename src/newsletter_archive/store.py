"""Writing files safely and loading/saving the manifest."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import ArchiveError
from .models import Manifest, ManifestEntry, utc_now_iso

MANIFEST_VERSION = 1


def write_text_atomic(path: Path, text: str) -> None:
    """Write text so readers see either the old file or the new one, never half of it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def dumps_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def write_json_atomic(path: Path, obj: Any) -> None:
    write_text_atomic(path, dumps_json(obj))


def load_manifest(path: Path) -> Manifest:
    path = Path(path)
    if not path.is_file():
        return Manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ArchiveError(f"{path} is not valid JSON ({e}); fix or delete it") from None
    entries: dict[str, ManifestEntry] = {}
    for item in data.get("posts", []):
        entry = ManifestEntry.from_dict(item)
        entries[entry.slug] = entry
    return Manifest(entries=entries)


def save_manifest(manifest: Manifest, path: Path) -> None:
    write_json_atomic(
        path,
        {
            "version": MANIFEST_VERSION,
            "updated_at": utc_now_iso(),
            "posts": [e.to_dict() for e in manifest.ordered()],
        },
    )
