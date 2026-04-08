import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from tools.code_intel.symbol_table import Reference, Symbol, SymbolTable


CACHE_DIR_NAME = ".lumakit"
CACHE_FILE = "code_index.json"


def _hash_file(path: str) -> str:
    """Fast content hash using md5 (not for security, just change detection)."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


class IndexCache:
    def __init__(self, project_root: Path):
        self.cache_dir = project_root / CACHE_DIR_NAME
        self.cache_path = self.cache_dir / CACHE_FILE

    def save(self, table: SymbolTable, references: list[Reference],
             file_hashes: dict[str, str]):
        """Persist the index to disk."""
        self.cache_dir.mkdir(exist_ok=True)

        data = {
            "version": 1,
            "file_hashes": file_hashes,
            "symbols": [asdict(s) for s in table.all_symbols()],
            "references": [asdict(r) for r in references],
        }

        # Write atomically via temp file
        tmp = self.cache_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            tmp.unlink(missing_ok=True)

    def load(self) -> tuple[SymbolTable, list[Reference], dict[str, str]] | None:
        """Load cached index. Returns None if cache doesn't exist or is corrupt."""
        if not self.cache_path.exists():
            return None

        try:
            raw = self.cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None

        if data.get("version") != 1:
            return None

        table = SymbolTable()
        for s in data.get("symbols", []):
            # Reconstruct Reference objects inside symbols
            refs = [Reference(**r) for r in s.pop("references", [])]
            sym = Symbol(**s)
            sym.references = refs
            table.add(sym)

        references = [Reference(**r) for r in data.get("references", [])]
        file_hashes = data.get("file_hashes", {})

        return table, references, file_hashes

    def get_stale_files(self, project_root: Path,
                        cached_hashes: dict[str, str],
                        current_files: list[str]) -> tuple[list[str], list[str]]:
        """Compare cached hashes against current files.

        Returns (changed_files, deleted_files) as relative paths.
        """
        current_set = set(current_files)
        cached_set = set(cached_hashes.keys())

        deleted = list(cached_set - current_set)
        changed = []

        for rel_path in current_files:
            abs_path = str(project_root / rel_path)
            current_hash = _hash_file(abs_path)
            if cached_hashes.get(rel_path) != current_hash:
                changed.append(rel_path)

        return changed, deleted

    @property
    def size_bytes(self) -> int:
        """Return the cache file size in bytes, 0 if missing."""
        try:
            return self.cache_path.stat().st_size
        except OSError:
            return 0

    def clear(self):
        """Delete the cache file."""
        self.cache_path.unlink(missing_ok=True)
