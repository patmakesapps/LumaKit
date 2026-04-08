import os
from pathlib import Path

from tools.code_intel.parsers import detect_language, get_snippet, parse_file
from tools.code_intel.symbol_table import Reference, SymbolTable

# Directories to skip during indexing
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".env",
             ".tox", ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs"}

# Max file size to parse (skip generated / vendored files)
MAX_FILE_SIZE = 512 * 1024  # 512 KB


class CodeIndex:
    def __init__(self, root: Path):
        self.root = root
        self.table = SymbolTable()
        self.references: list[Reference] = []
        self._file_hashes: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def build(self):
        """Full index of the project."""
        self.table = SymbolTable()
        self.references = []
        self._file_hashes = {}

        for file_path in self._walk_files():
            self._index_file(file_path)

    def update_file(self, file_path: str):
        """Re-index a single file (after edit/write/delete)."""
        rel = self._rel(file_path)
        self.table.remove_file(rel)
        self.references = [r for r in self.references if r.file != rel]

        abs_path = self.root / rel
        if abs_path.exists():
            self._index_file(str(abs_path))

    def _index_file(self, abs_path: str):
        rel = self._rel(abs_path)
        if detect_language(rel) is None:
            return

        try:
            size = os.path.getsize(abs_path)
            if size > MAX_FILE_SIZE:
                return
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        symbols, refs = parse_file(rel, source)
        for sym in symbols:
            self.table.add(sym)
        self.references.extend(refs)

    def _walk_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                if detect_language(fname) is not None:
                    yield full

    def _rel(self, path: str) -> str:
        try:
            return str(Path(path).relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def find_definition(self, symbol: str, language: str | None = None,
                        kind: str | None = None) -> list[dict]:
        matches = self.table.lookup(symbol)
        if kind:
            matches = [s for s in matches if s.kind == kind]
        if language:
            matches = [s for s in matches if detect_language(s.file) == language]

        results = []
        for sym in matches:
            try:
                snippet = get_snippet(
                    str(self.root / sym.file), sym.line, sym.end_line
                )
            except OSError:
                snippet = ""

            results.append({
                "symbol": sym.name,
                "qualified_name": sym.qualified_name,
                "kind": sym.kind,
                "file": sym.file,
                "line": sym.line,
                "end_line": sym.end_line,
                "params": sym.params or None,
                "return_type": sym.return_type,
                "docstring": sym.docstring,
                "parent": sym.parent,
                "snippet": snippet,
            })
        return results

    def find_usages(self, symbol: str, kind: str | None = None) -> list[dict]:
        """Find all references to a symbol across the codebase."""
        usages = []

        # 1. Search import references
        needle_lower = symbol.lower()
        for ref in self.references:
            if needle_lower in ref.context.lower():
                usages.append({
                    "file": ref.file,
                    "line": ref.line,
                    "kind": ref.kind,
                    "context": ref.context,
                })

        # 2. Scan files for call-site / attribute references
        for file_path in self._walk_files():
            rel = self._rel(file_path)
            try:
                source = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for line_num, line in enumerate(source.splitlines(), start=1):
                # Skip lines already captured as imports
                if any(u["file"] == rel and u["line"] == line_num for u in usages):
                    continue
                if symbol in line:
                    # Determine usage kind from context
                    stripped = line.strip()
                    if f"import {symbol}" in stripped or f"from " in stripped:
                        continue  # already captured
                    elif f"{symbol}(" in stripped:
                        use_kind = "call"
                    elif f".{symbol}" in stripped:
                        use_kind = "attribute"
                    elif f"{symbol} =" in stripped or f"{symbol}:" in stripped:
                        use_kind = "assignment"
                    else:
                        use_kind = "reference"

                    if kind and use_kind != kind:
                        continue

                    usages.append({
                        "file": rel,
                        "line": line_num,
                        "kind": use_kind,
                        "context": stripped,
                    })

        return usages

    def get_file_structure(self, file_path: str) -> dict:
        """Return a table-of-contents for a file."""
        rel = self._rel(file_path)
        symbols = self.table.get_file_symbols(rel)

        # Also get imports for this file
        file_imports = [r.context for r in self.references
                        if r.file == rel and r.kind == "import"]

        language = detect_language(rel)

        # Build hierarchical structure: classes with their methods
        top_level = []
        children_map: dict[str, list] = {}

        for sym in sorted(symbols, key=lambda s: s.line):
            entry = {
                "name": sym.name,
                "kind": sym.kind,
                "line": sym.line,
            }
            if sym.params:
                entry["params"] = sym.params
            if sym.return_type:
                entry["returns"] = sym.return_type

            if sym.parent:
                children_map.setdefault(sym.parent, []).append(entry)
            else:
                top_level.append(entry)

        # Attach children to their parent classes
        for item in top_level:
            qn = f"{rel.replace('/', '.').removesuffix('.py').removesuffix('.js').removesuffix('.ts').removesuffix('.tsx')}.{item['name']}"
            kids = children_map.get(qn, [])
            if kids:
                item["children"] = kids

        return {
            "file": rel,
            "language": language,
            "imports": file_imports,
            "symbols": top_level,
        }

    def search_symbols(self, query: str, kind: str | None = None,
                       limit: int = 20) -> list[dict]:
        results = self.table.search(query, kind=kind, limit=limit)
        return [
            {
                "name": sym.name,
                "qualified_name": sym.qualified_name,
                "kind": sym.kind,
                "file": sym.file,
                "line": sym.line,
                "score": score,
            }
            for sym, score in results
        ]

    # ------------------------------------------------------------------
    # Tool exports
    # ------------------------------------------------------------------

    def get_tools(self) -> list[dict]:
        return [
            get_find_definition_tool(self),
            get_find_usages_tool(self),
            get_file_structure_tool(self),
            get_search_symbols_tool(self),
        ]


# ======================================================================
# Tool factory functions (match LumaKit's get_*_tool() pattern)
# ======================================================================

def get_find_definition_tool(index: CodeIndex):
    def _execute(inputs):
        results = index.find_definition(
            symbol=inputs["symbol"],
            language=inputs.get("language"),
            kind=inputs.get("kind"),
        )
        if not results:
            return {"symbol": inputs["symbol"], "found": False, "results": []}
        return {"symbol": inputs["symbol"], "found": True, "count": len(results), "results": results}

    return {
        "name": "find_definition",
        "description": (
            "Find where a symbol (function, class, method, variable) is defined in the codebase. "
            "Returns the file, line number, parameters, docstring, and a code snippet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Name of the symbol to find"},
                "language": {"type": "string", "description": "Filter by language (python, javascript, typescript)"},
                "kind": {"type": "string", "description": "Filter by kind (function, class, method, variable)"},
            },
            "required": ["symbol"],
        },
        "execute": _execute,
    }


def get_find_usages_tool(index: CodeIndex):
    def _execute(inputs):
        usages = index.find_usages(
            symbol=inputs["symbol"],
            kind=inputs.get("kind"),
        )
        return {
            "symbol": inputs["symbol"],
            "total": len(usages),
            "usages": usages[:50],  # cap output size
            "truncated": len(usages) > 50,
        }

    return {
        "name": "find_usages",
        "description": (
            "Find all places where a symbol is used across the codebase. "
            "Returns file, line, usage kind (call, import, assignment, attribute, reference), and context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Name of the symbol to search for"},
                "kind": {"type": "string", "description": "Filter by usage kind (call, import, assignment, attribute)"},
            },
            "required": ["symbol"],
        },
        "execute": _execute,
    }


def get_file_structure_tool(index: CodeIndex):
    def _execute(inputs):
        return index.get_file_structure(inputs["path"])

    return {
        "name": "get_file_structure",
        "description": (
            "Get the structure of a source file: its imports, classes, functions, methods, "
            "and variables with line numbers. Like a table of contents — avoids reading the whole file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to project root)"},
            },
            "required": ["path"],
        },
        "execute": _execute,
    }


def get_search_symbols_tool(index: CodeIndex):
    def _execute(inputs):
        results = index.search_symbols(
            query=inputs["query"],
            kind=inputs.get("kind"),
            limit=int(inputs.get("limit", 20)),
        )
        return {"query": inputs["query"], "count": len(results), "results": results}

    return {
        "name": "search_symbols",
        "description": (
            "Fuzzy search for symbols (functions, classes, methods, variables) by name. "
            "Returns matches ranked by relevance with file and line info."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (partial name match)"},
                "kind": {"type": "string", "description": "Filter by kind (function, class, method, variable)"},
                "limit": {"type": "integer", "description": "Max results to return (default 20)"},
            },
            "required": ["query"],
        },
        "execute": _execute,
    }
