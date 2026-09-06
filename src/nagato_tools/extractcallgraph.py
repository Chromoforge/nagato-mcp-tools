import ast
from collections import defaultdict
from pathlib import Path


class CallgraphBuilder(ast.NodeVisitor):
    def __init__(self, file_path: str):
        """
        Initializes the CallgraphBuilder for a single Python file.

        Parameters
        ----------
        file_path : str
            Path to the source file whose AST should be analyzed.

        Attributes
        ----------
        current_scope : list[str]
            Stack of enclosing scopes (e.g., ["MyClass", "my_method"]).
        calls : dict[str, list[str]]
            Mapping: Symbol → List of called functions/methods.
        imports : dict[str, list[str]]
            Mapping: Symbol → List of imports used within this symbol.
        """
        self.file_path = file_path
        self.scope_stack = []
        self.calls = defaultdict(list)
        self.imports = defaultdict(list)

    @property
    def current_symbol(self) -> str:
        """
        Returns the fully qualified symbol name for the current scope,
        or '<global>' for module-level statements.
        """
        if not self.scope_stack:
            return "<global>"
        return ".".join(self.scope_stack)

    def build(self):
        """
        Parses the file, traverses the AST, and produces call and import graphs.

        Returns
        -------
        dict
            Structure:
            {
                "file": <path>,
                "calls": {symbol: [callee, ...]},
                "imports": {symbol: [import, ...]},
            }
        """
        source = Path(self.file_path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        self.visit(tree)

        # Invert for the reverse call graph (called_from)
        called_from = defaultdict(list)
        for caller, callees in self.calls.items():
            for callee in callees:
                # Prevent duplicates if a function is called multiple times
                if caller not in called_from[callee]:
                    called_from[callee].append(caller)

        return {
            "file": self.file_path,
            "calls": dict(self.calls),
            "called_from": dict(called_from),
            "imports": dict(self.imports),
        }

    # --- Symbol boundaries ----------------------------------------------------

    def visit_FunctionDef(self, node):
        """
        Enters a function definition and pushes to the scope stack.
        """
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node):
        """
        Like visit_FunctionDef, but for async functions.
        """
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_ClassDef(self, node):
        """
        Enters a class definition and pushes to the scope stack.
        """
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    # --- Call detection -------------------------------------------------------

    def visit_Call(self, node):
        """
        Captures function and method calls within the current symbol scope.
        """
        callee = self._extract_callee(node.func)
        if callee:
            self.calls[self.current_symbol].append(callee)

        self.generic_visit(node)

    def _extract_callee(self, func):
        """
        Extracts the name of the called symbol from a Call node.

        Returns
        -------
        str | None
            Function name or attribute chain (e.g. "obj.method").
        """
        # direct call: foo()
        if isinstance(func, ast.Name):
            return func.id

        # attribute call: obj.method()
        if isinstance(func, ast.Attribute):
            return f"{self._expr_to_str(func.value)}.{func.attr}"

        return None

    # --- Import detection -----------------------------------------------------

    def visit_Import(self, node):
        """
        Captures `import x`-Statements in current scope.
        """
        for alias in node.names:
            self.imports[self.current_symbol].append(alias.name)

    def visit_ImportFrom(self, node):
        """
        Captures `from x import y`-Statements in current scope.
        """
        module = node.module or ""
        for alias in node.names:
            self.imports[self.current_symbol].append(f"{module}.{alias.name}")

    # --- Helpers --------------------------------------------------------------

    def _expr_to_str(self, expr):
        """
        Converts an AST expression into Python source code.

        Returns
        -------
        str | None
            Unparsed expression or None on errors.
        """
        try:
            return ast.unparse(expr)
        except Exception:
            return None
__all__ = []
