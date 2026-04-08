import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")

import tree_sitter_languages as tsl

from tools.code_intel.symbol_table import Reference, Symbol, SymbolTable


LANG_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
}

# Tree-sitter queries per language for extracting definitions.
# Each entry: (query_string, symbol_kind)
# Captures named @name are the symbol name, @definition is the full node.
LANG_QUERIES = {
    "python": {
        "classes": "(class_definition name: (identifier) @name) @definition",
        "functions": "(function_definition name: (identifier) @name) @definition",
        "imports": [
            "(import_statement) @imp",
            "(import_from_statement) @imp",
        ],
        "assignments": "(assignment left: (identifier) @name) @definition",
    },
    "javascript": {
        "classes": "(class_declaration name: (identifier) @name) @definition",
        "functions": "(function_declaration name: (identifier) @name) @definition",
        "methods": "(method_definition name: (property_identifier) @name) @definition",
        "arrows": "(lexical_declaration (variable_declarator name: (identifier) @name value: (arrow_function) @definition))",
        "imports": [
            "(import_statement) @imp",
        ],
        "assignments": "(variable_declarator name: (identifier) @name) @definition",
    },
    "typescript": {
        "classes": "(class_declaration name: (type_identifier) @name) @definition",
        "functions": "(function_declaration name: (identifier) @name) @definition",
        "methods": "(method_definition name: (property_identifier) @name) @definition",
        "arrows": "(lexical_declaration (variable_declarator name: (identifier) @name value: (arrow_function) @definition))",
        "imports": [
            "(import_statement) @imp",
        ],
        "assignments": "(variable_declarator name: (identifier) @name) @definition",
    },
    "go": {
        "structs": "(type_declaration (type_spec name: (type_identifier) @name type: (struct_type)) @definition)",
        "interfaces": "(type_declaration (type_spec name: (type_identifier) @name type: (interface_type)) @definition)",
        "functions": "(function_declaration name: (identifier) @name) @definition",
        "go_methods": "(method_declaration name: (field_identifier) @name) @definition",
        "imports": [
            "(import_declaration) @imp",
        ],
        "go_vars": "(var_declaration (var_spec name: (identifier) @name)) @definition",
        "go_consts": "(const_declaration (const_spec name: (identifier) @name)) @definition",
    },
    "rust": {
        "structs": "(struct_item name: (type_identifier) @name) @definition",
        "enums": "(enum_item name: (type_identifier) @name) @definition",
        "traits": "(trait_item name: (type_identifier) @name) @definition",
        "functions": "(function_item name: (identifier) @name) @definition",
        "imports": [
            "(use_declaration) @imp",
        ],
        "rust_consts": "(const_item name: (identifier) @name) @definition",
        "rust_statics": "(static_item name: (identifier) @name) @definition",
    },
}


def detect_language(file_path: str) -> str | None:
    ext = Path(file_path).suffix.lower()
    return LANG_MAP.get(ext)


def _get_parser(language: str):
    return tsl.get_parser(language)


def _get_language(language: str):
    return tsl.get_language(language)


def _extract_params_python(node) -> list[str]:
    """Extract parameter names from a Python function_definition node."""
    for child in node.children:
        if child.type == "parameters":
            params = []
            for p in child.children:
                if p.type in ("identifier", "typed_parameter", "default_parameter",
                              "typed_default_parameter", "list_splat_pattern",
                              "dictionary_splat_pattern"):
                    text = p.text.decode("utf-8")
                    if text not in ("(", ")", ","):
                        params.append(text)
            return params
    return []


def _extract_params_js(node) -> list[str]:
    """Extract parameter names from a JS/TS function or method node."""
    for child in node.children:
        if child.type in ("formal_parameters", "required_parameter",
                          "optional_parameter"):
            params = []
            for p in child.children:
                if p.type == "identifier":
                    params.append(p.text.decode("utf-8"))
                elif p.type in ("required_parameter", "optional_parameter"):
                    for sub in p.children:
                        if sub.type == "identifier":
                            params.append(sub.text.decode("utf-8"))
                            break
            return params
    return []


def _extract_return_type_python(node) -> str | None:
    """Extract return type annotation from a Python function_definition."""
    for child in node.children:
        if child.type == "type":
            return child.text.decode("utf-8")
    return None


def _extract_docstring_python(node) -> str | None:
    """Extract docstring from the first expression_statement in a Python body."""
    for child in node.children:
        if child.type == "block":
            for stmt in child.children:
                if stmt.type == "expression_statement":
                    for expr in stmt.children:
                        if expr.type == "string":
                            raw = expr.text.decode("utf-8")
                            return raw.strip("\"'").strip()
                    break
            break
    return None


def _find_parent_class(node) -> str | None:
    """Walk up the tree to find an enclosing class/struct/impl name."""
    current = node.parent
    while current:
        if current.type in ("class_definition", "class_declaration"):
            for child in current.children:
                if child.type in ("identifier", "type_identifier"):
                    return child.text.decode("utf-8")
        # Rust: function inside impl block
        if current.type == "impl_item":
            for child in current.children:
                if child.type == "type_identifier":
                    return child.text.decode("utf-8")
        current = current.parent
    return None


def _extract_go_receiver(node) -> str | None:
    """Extract receiver type from a Go method_declaration."""
    for child in node.children:
        if child.type == "parameter_list":
            for p in child.children:
                if p.type == "parameter_declaration":
                    for t in p.children:
                        if t.type == "pointer_type":
                            for inner in t.children:
                                if inner.type == "type_identifier":
                                    return inner.text.decode("utf-8")
                        elif t.type == "type_identifier":
                            return t.text.decode("utf-8")
            break
    return None


def _extract_go_params(node) -> list[str]:
    """Extract parameter list from a Go function/method declaration."""
    params = []
    param_lists = [c for c in node.children if c.type == "parameter_list"]
    # For methods, skip the first parameter_list (receiver)
    target = param_lists[1] if len(param_lists) > 1 else (param_lists[0] if param_lists else None)
    if not target:
        return params
    for child in target.children:
        if child.type == "parameter_declaration":
            params.append(child.text.decode("utf-8"))
    return params


def _extract_rust_params(node) -> list[str]:
    """Extract parameter list from a Rust function_item."""
    params = []
    for child in node.children:
        if child.type == "parameters":
            for p in child.children:
                if p.type in ("parameter", "self_parameter"):
                    params.append(p.text.decode("utf-8"))
            break
    return params


def _snippet(source_lines: list[str], start: int, end: int, max_lines: int = 8) -> str:
    """Return a context snippet, truncated if the definition is long."""
    lines = source_lines[start:end + 1]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + "\n    ..."


def parse_file(file_path: str, source: str | None = None) -> tuple[list[Symbol], list[Reference]]:
    """Parse a single file and return extracted symbols and references."""
    language = detect_language(file_path)
    if language is None:
        return [], []

    if source is None:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")

    source_bytes = source.encode("utf-8")
    source_lines = source.splitlines()

    parser = _get_parser(language)
    lang = _get_language(language)
    tree = parser.parse(source_bytes)

    queries = LANG_QUERIES.get(language, {})
    if not queries:
        return [], []

    symbols = []
    references = []

    is_python = language == "python"
    is_go = language == "go"
    is_rust = language == "rust"

    if is_python:
        extract_params = _extract_params_python
    elif is_go:
        extract_params = _extract_go_params
    elif is_rust:
        extract_params = _extract_rust_params
    else:
        extract_params = _extract_params_js

    # Module prefix for qualified names
    module = file_path.replace("\\", "/").replace("/", ".")
    for suffix in (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs"):
        module = module.removesuffix(suffix)

    # --- Extract classes ---
    if "classes" in queries:
        q = lang.query(queries["classes"])
        captures = q.captures(tree.root_node)
        for node, capture_name in captures:
            if capture_name != "definition":
                continue
            # Extract name from the definition node's children
            class_name = None
            for child in node.children:
                if child.type in ("identifier", "type_identifier"):
                    class_name = child.text.decode("utf-8")
                    break
            if not class_name:
                continue

            docstring = _extract_docstring_python(node) if is_python else None
            symbols.append(Symbol(
                name=class_name,
                qualified_name=f"{module}.{class_name}",
                kind="class",
                file=file_path,
                line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                docstring=docstring,
            ))

    # --- Extract structs (Go/Rust) ---
    for query_key, sym_kind in [("structs", "class"), ("interfaces", "class"),
                                 ("enums", "class"), ("traits", "class")]:
        if query_key in queries:
            q = lang.query(queries[query_key])
            captures = q.captures(tree.root_node)
            for node, capture_name in captures:
                if capture_name != "definition":
                    continue
                type_name = None
                for child in node.children:
                    if child.type in ("type_identifier", "type_spec"):
                        if child.type == "type_spec":
                            for sub in child.children:
                                if sub.type == "type_identifier":
                                    type_name = sub.text.decode("utf-8")
                                    break
                        else:
                            type_name = child.text.decode("utf-8")
                        break
                if not type_name:
                    continue
                symbols.append(Symbol(
                    name=type_name,
                    qualified_name=f"{module}.{type_name}",
                    kind=sym_kind,
                    file=file_path,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                ))

    # --- Extract functions ---
    if "functions" in queries:
        q = lang.query(queries["functions"])
        captures = q.captures(tree.root_node)
        for node, capture_name in captures:
            if capture_name != "definition":
                continue
            func_name = None
            for child in node.children:
                if child.type == "identifier":
                    func_name = child.text.decode("utf-8")
                    break
            if not func_name:
                continue

            parent_class = _find_parent_class(node)
            kind = "method" if parent_class else "function"
            parent_qn = f"{module}.{parent_class}" if parent_class else None
            qn = f"{parent_qn}.{func_name}" if parent_qn else f"{module}.{func_name}"

            params = extract_params(node)
            return_type = _extract_return_type_python(node) if is_python else None
            docstring = _extract_docstring_python(node) if is_python else None

            symbols.append(Symbol(
                name=func_name,
                qualified_name=qn,
                kind=kind,
                file=file_path,
                line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent=parent_qn,
                params=params,
                return_type=return_type,
                docstring=docstring,
            ))

    # --- Extract Go methods (with receiver) ---
    if "go_methods" in queries:
        q = lang.query(queries["go_methods"])
        captures = q.captures(tree.root_node)
        for node, capture_name in captures:
            if capture_name != "definition":
                continue
            method_name = None
            for child in node.children:
                if child.type == "field_identifier":
                    method_name = child.text.decode("utf-8")
                    break
            if not method_name:
                continue

            receiver = _extract_go_receiver(node)
            parent_qn = f"{module}.{receiver}" if receiver else None
            qn = f"{parent_qn}.{method_name}" if parent_qn else f"{module}.{method_name}"
            params = _extract_go_params(node)

            symbols.append(Symbol(
                name=method_name,
                qualified_name=qn,
                kind="method",
                file=file_path,
                line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent=parent_qn,
                params=params,
            ))

    # --- Extract methods (JS/TS) ---
    if "methods" in queries:
        q = lang.query(queries["methods"])
        captures = q.captures(tree.root_node)
        for node, capture_name in captures:
            if capture_name != "definition":
                continue
            method_name = None
            for child in node.children:
                if child.type == "property_identifier":
                    method_name = child.text.decode("utf-8")
                    break
            if not method_name:
                continue

            parent_class = _find_parent_class(node)
            parent_qn = f"{module}.{parent_class}" if parent_class else None
            qn = f"{parent_qn}.{method_name}" if parent_qn else f"{module}.{method_name}"

            params = extract_params(node)
            symbols.append(Symbol(
                name=method_name,
                qualified_name=qn,
                kind="method",
                file=file_path,
                line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent=parent_qn,
                params=params,
            ))

    # --- Extract arrow functions (JS/TS) ---
    if "arrows" in queries:
        q = lang.query(queries["arrows"])
        captures = q.captures(tree.root_node)
        for node, capture_name in captures:
            if capture_name == "name":
                symbols.append(Symbol(
                    name=node.text.decode("utf-8"),
                    qualified_name=f"{module}.{node.text.decode('utf-8')}",
                    kind="function",
                    file=file_path,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                ))

    # --- Extract imports as references ---
    if "imports" in queries:
        import_queries = queries["imports"]
        if isinstance(import_queries, str):
            import_queries = [import_queries]
        for iq in import_queries:
            q = lang.query(iq)
            captures = q.captures(tree.root_node)
            for node, capture_name in captures:
                line_num = node.start_point[0] + 1
                text = node.text.decode("utf-8").strip()
                references.append(Reference(
                    file=file_path,
                    line=line_num,
                    kind="import",
                    context=text,
                ))

    # --- Extract top-level assignments (variables/constants) ---
    if "assignments" in queries:
        q = lang.query(queries["assignments"])
        captures = q.captures(tree.root_node)
        for node, capture_name in captures:
            if capture_name == "name":
                # Only top-level assignments (parent is module/program)
                def_parent = node.parent
                while def_parent and def_parent.type in ("assignment", "variable_declarator",
                                                          "lexical_declaration", "variable_declaration",
                                                          "expression_statement"):
                    def_parent = def_parent.parent
                if def_parent and def_parent.type in ("module", "program"):
                    var_name = node.text.decode("utf-8")
                    symbols.append(Symbol(
                        name=var_name,
                        qualified_name=f"{module}.{var_name}",
                        kind="variable",
                        file=file_path,
                        line=node.start_point[0] + 1,
                        end_line=node.start_point[0] + 1,
                    ))

    # --- Extract Go/Rust vars, consts, statics ---
    for query_key in ("go_vars", "go_consts", "rust_consts", "rust_statics"):
        if query_key in queries:
            q = lang.query(queries[query_key])
            captures = q.captures(tree.root_node)
            for node, capture_name in captures:
                if capture_name == "name":
                    var_name = node.text.decode("utf-8")
                    symbols.append(Symbol(
                        name=var_name,
                        qualified_name=f"{module}.{var_name}",
                        kind="variable",
                        file=file_path,
                        line=node.start_point[0] + 1,
                        end_line=node.start_point[0] + 1,
                    ))

    return symbols, references


def get_snippet(file_path: str, start_line: int, end_line: int, max_lines: int = 8) -> str:
    """Read a file and return a snippet for the given line range."""
    source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines()
    return _snippet(lines, start_line - 1, end_line - 1, max_lines)
