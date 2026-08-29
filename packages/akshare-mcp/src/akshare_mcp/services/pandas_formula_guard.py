"""AST whitelist for the pandas-expression factor formulas used by llm_alpha.

Why this exists as its own validator
------------------------------------
``factor_candidate_compiler`` validates the *DSL* dialect: bare field names and
whitelisted helper calls such as ``ts_mean(close, 20)``. Its whitelist
deliberately excludes ``ast.Attribute``, so it can never accept a pandas method
chain like ``close.pct_change(60)``.

The local candidate pool in ``llm_alpha`` is built from pandas method chains, and
those formulas were being handed straight to ``eval`` on the raw string. Emptying
``__builtins__`` does not contain that: attribute access alone is enough to walk
out via ``().__class__.__base__.__subclasses__()``. Reusing the DSL compiler here
would reject every formula and silently disable IC ranking, so this module
supplies the missing dialect instead: a narrow attribute whitelist covering only
the pandas methods those templates actually use.

Rejected by construction: dunder and any non-whitelisted attribute, subscripts,
comprehensions, lambdas, imports, comparisons, boolean/bitwise operators,
starred args, f-strings, walrus, and non-numeric constants.
"""

from __future__ import annotations

import ast
from types import CodeType

# pandas methods reachable on a Series inside a formula. Kept to what the
# templates in llm_alpha actually use; widen deliberately, never speculatively.
ALLOWED_SERIES_METHODS: frozenset[str] = frozenset(
    {
        "abs",
        "clip",
        "diff",
        "ewm",
        "max",
        "mean",
        "median",
        "min",
        "pct_change",
        "rank",
        "rolling",
        "shift",
        "std",
        "sum",
        "var",
    }
)

# Bare functions injected into the evaluation namespace.
ALLOWED_FUNCTIONS: frozenset[str] = frozenset({"abs", "log1p", "sign", "sqrt"})

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Attribute,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.keyword,
)

_ALLOWED_BINOPS: tuple[type[ast.operator], ...] = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
)

_ALLOWED_UNARYOPS: tuple[type[ast.unaryop], ...] = (ast.UAdd, ast.USub)

MAX_FORMULA_LENGTH = 600
MAX_NODE_COUNT = 200
MAX_DEPTH = 20


class FormulaRejected(ValueError):
    """A formula failed the pandas-expression whitelist.

    ``code`` is a stable machine-readable reason so callers and tests can assert
    *why* a formula was rejected rather than only that it was.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _Validator:
    def __init__(self, allowed_names: frozenset[str]) -> None:
        self.allowed_names = allowed_names
        self.node_count = 0

    def visit(self, node: ast.AST, depth: int = 0) -> None:
        self.node_count += 1
        if self.node_count > MAX_NODE_COUNT:
            raise FormulaRejected("too_complex", f"formula exceeds {MAX_NODE_COUNT} AST nodes")
        if depth > MAX_DEPTH:
            raise FormulaRejected("too_deep", f"formula nests deeper than {MAX_DEPTH}")
        if not isinstance(node, _ALLOWED_NODES):
            raise FormulaRejected("unsupported_node", f"unsupported AST node: {type(node).__name__}")

        if isinstance(node, ast.Expression):
            self.visit(node.body, depth + 1)
            return

        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                raise FormulaRejected("unsupported_operator", f"unsupported binary operator: {type(node.op).__name__}")
            self.visit(node.left, depth + 1)
            self.visit(node.right, depth + 1)
            return

        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARYOPS):
                raise FormulaRejected("unsupported_operator", f"unsupported unary operator: {type(node.op).__name__}")
            self.visit(node.operand, depth + 1)
            return

        if isinstance(node, ast.Attribute):
            attr = str(node.attr or "")
            if attr.startswith("_"):
                # Blocks the classic __class__/__subclasses__ escape outright.
                raise FormulaRejected("private_attribute", f"private attribute access is not allowed: {attr!r}")
            if attr not in ALLOWED_SERIES_METHODS:
                raise FormulaRejected("unsupported_attribute", f"attribute is not whitelisted: {attr!r}")
            self.visit(node.value, depth + 1)
            return

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                self.visit(func, depth + 1)
            elif isinstance(func, ast.Name):
                name = str(func.id or "")
                if name not in ALLOWED_FUNCTIONS:
                    raise FormulaRejected("unsupported_function", f"function is not whitelisted: {name!r}")
            else:
                raise FormulaRejected("unsupported_call", "only whitelisted names and methods may be called")
            for arg in node.args:
                if isinstance(arg, ast.Starred):
                    raise FormulaRejected("unsupported_node", "starred arguments are not allowed")
                self.visit(arg, depth + 1)
            for kw in node.keywords:
                if kw.arg is None:
                    raise FormulaRejected("unsupported_node", "**kwargs expansion is not allowed")
                self.visit(kw.value, depth + 1)
            return

        if isinstance(node, ast.Name):
            name = str(node.id or "")
            if name in ALLOWED_FUNCTIONS:
                return
            if name not in self.allowed_names:
                raise FormulaRejected("unknown_name", f"name is not an available column: {name!r}")
            return

        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaRejected("non_numeric_constant", "only numeric constants are allowed")
            return

        if isinstance(node, ast.keyword):
            self.visit(node.value, depth + 1)
            return


def compile_pandas_formula(formula: str, *, allowed_names: frozenset[str] | set[str]) -> CodeType:
    """Validate a pandas expression against the whitelist and compile it.

    Returns a code object built from the *validated* tree, so callers eval a
    verified AST rather than an arbitrary string.

    Raises ``FormulaRejected`` with a stable ``.code`` for every refusal.
    """
    text = str(formula or "").strip()
    if not text:
        raise FormulaRejected("empty", "formula is empty")
    if len(text) > MAX_FORMULA_LENGTH:
        raise FormulaRejected("too_long", f"formula exceeds {MAX_FORMULA_LENGTH} characters")
    if "\n" in text or "\r" in text:
        raise FormulaRejected("multiline", "formula must be a single expression")

    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        # mode="eval" already rejects statements, assignments and semicolons.
        raise FormulaRejected("syntax_error", str(exc)) from exc

    _Validator(frozenset(allowed_names)).visit(tree)
    return compile(tree, filename="<pandas_formula>", mode="eval")


__all__ = [
    "ALLOWED_FUNCTIONS",
    "ALLOWED_SERIES_METHODS",
    "FormulaRejected",
    "compile_pandas_formula",
]
