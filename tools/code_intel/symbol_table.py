from dataclasses import dataclass, field


@dataclass
class Reference:
    file: str
    line: int
    kind: str       # "call", "import", "assignment", "attribute"
    context: str    # the source line trimmed


@dataclass
class Symbol:
    name: str
    qualified_name: str         # e.g. "core.session.UserSession.__init__"
    kind: str                   # "function", "class", "method", "variable", "import"
    file: str
    line: int
    end_line: int
    parent: str | None = None   # enclosing class/function qualified name
    params: list[str] = field(default_factory=list)
    return_type: str | None = None
    docstring: str | None = None
    references: list[Reference] = field(default_factory=list)


class SymbolTable:
    def __init__(self):
        self.symbols: dict[str, Symbol] = {}        # qualified_name -> Symbol
        self._by_name: dict[str, list[str]] = {}    # short name -> [qualified_names]
        self._by_file: dict[str, list[str]] = {}    # file path -> [qualified_names]

    def add(self, symbol: Symbol):
        self.symbols[symbol.qualified_name] = symbol
        self._by_name.setdefault(symbol.name, []).append(symbol.qualified_name)
        self._by_file.setdefault(symbol.file, []).append(symbol.qualified_name)

    def remove_file(self, file_path: str):
        qnames = self._by_file.pop(file_path, [])
        for qn in qnames:
            sym = self.symbols.pop(qn, None)
            if sym and sym.name in self._by_name:
                self._by_name[sym.name] = [
                    q for q in self._by_name[sym.name] if q != qn
                ]
                if not self._by_name[sym.name]:
                    del self._by_name[sym.name]

    def lookup(self, name: str) -> list[Symbol]:
        return [self.symbols[qn] for qn in self._by_name.get(name, []) if qn in self.symbols]

    def lookup_qualified(self, qualified_name: str) -> Symbol | None:
        return self.symbols.get(qualified_name)

    def get_file_symbols(self, file_path: str) -> list[Symbol]:
        return [self.symbols[qn] for qn in self._by_file.get(file_path, []) if qn in self.symbols]

    def search(self, query: str, kind: str | None = None, limit: int = 20) -> list[tuple[Symbol, float]]:
        query_lower = query.lower()
        results = []
        for sym in self.symbols.values():
            if kind and sym.kind != kind:
                continue
            name_lower = sym.name.lower()
            if query_lower == name_lower:
                score = 1.0
            elif query_lower in name_lower:
                score = len(query_lower) / len(name_lower)
            elif name_lower in query_lower:
                score = len(name_lower) / len(query_lower) * 0.5
            else:
                continue
            results.append((sym, round(score, 2)))
        results.sort(key=lambda x: -x[1])
        return results[:limit]

    def all_symbols(self) -> list[Symbol]:
        return list(self.symbols.values())
