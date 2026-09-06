import ast
from pathlib import Path


class SignatureExtractor(ast.NodeVisitor):
    def __init__(self, file_path: str):
        """
        Initializes the SignatureExtractor for a Python file.

        Parameters
        ----------
        file_path : str
            Path to the source file whose signatures should be extracted.

        Attributes
        ----------
        signatures : list[dict]
            Collected function, class, and async signatures.
        """
        self.file_path = file_path
        self.signatures = []

    def extract(self):
        """
        Parses the file and extracts all signatures.

        Returns
        -------
        list[dict]
            List of signature dictionaries with symbol name, kind, args, return,
            decorators, and docstring (first line).
        """
        source = Path(self.file_path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        self.visit(tree)
        return self.signatures

    def visit_FunctionDef(self, node):
        """
        Captures a function signature.
        """
        self._add_signature(node, "function")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        """
        Captures an async function signature.
        """
        self._add_signature(node, "async_function")
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        """
        Captures a class signature.
        """
        self._add_signature(node, "class")
        self.generic_visit(node)

    def _add_signature(self, node, kind):
        """
        Builds a signature dictionary from an AST node.

        Parameters
        ----------
        node : ast.AST
            Function or class node.
        kind : str
            Kind of symbol: "function", "async_function", or "class".
        """
        args = []
        if kind == "class":
            # For class, extract __init__ args if available
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
                    args = self._extract_args(item)
                    break
        elif hasattr(node, "args"):
            args = self._extract_args(node)

        sig = {
            "symbol": node.name,
            "kind": kind,
            "file": self.file_path,
            "line": node.lineno,
            "args": args,
            "returns": self._extract_return(node) if kind != "class" else None,
            "decorators": [self._expr_to_str(d) for d in getattr(node, "decorator_list", [])],
            "doc": self._extract_doc(node),
        }
        self.signatures.append(sig)

    def _extract_args(self, node):
        """
        Extracts function arguments including positional, kwonly, *args, and **kwargs with annotations.

        Returns
        -------
        list[dict]
            List of {"name": ..., "annotation": ...}.
        """
        args = []
        # Standard positional and positional-or-keyword args
        for arg in node.args.args:
            args.append({
                "name": arg.arg,
                "annotation": self._expr_to_str(arg.annotation),
            })
        
        # Positional-only args (Python 3.8+)
        for arg in getattr(node.args, "posonlyargs", []):
            args.append({
                "name": arg.arg,
                "annotation": self._expr_to_str(arg.annotation),
            })

        # *args (vararg)
        if node.args.vararg:
            args.append({
                "name": f"*{node.args.vararg.arg}",
                "annotation": self._expr_to_str(node.args.vararg.annotation),
            })

        # Keyword-only args
        for arg in node.args.kwonlyargs:
            args.append({
                "name": arg.arg,
                "annotation": self._expr_to_str(arg.annotation),
            })

        # **kwargs (kwarg)
        if node.args.kwarg:
            args.append({
                "name": f"**{node.args.kwarg.arg}",
                "annotation": self._expr_to_str(node.args.kwarg.annotation),
            })

        return args

    def _extract_return(self, node):
        """
        Extracts the return annotation of a function.

        Returns
        -------
        str | None
        """
        return self._expr_to_str(getattr(node, "returns", None))

    def _extract_doc(self, node):
        """
        Extracts the docstring (first line only, max 200 chars).

        Returns
        -------
        str | None
        """
        doc = ast.get_docstring(node)
        if not doc:
            return None
        return doc.strip().split("\n")[0][:200]

    def _expr_to_str(self, expr):
        """
        Converts an AST expression into Python code.

        Returns
        -------
        str | None
        """
        if expr is None:
            return None
        try:
            return ast.unparse(expr)
        except Exception:
            return None
__all__ = []
