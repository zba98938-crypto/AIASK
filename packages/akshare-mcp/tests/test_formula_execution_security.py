"""Formula execution security: AST whitelists must actually refuse things.

Three separate holes are pinned here.

1. ``pandas_formula_guard`` — ``llm_alpha`` used to ``eval`` raw formula strings
   with only ``{"__builtins__": {}}`` as protection. That does not stop attribute
   walking (``().__class__.__base__.__subclasses__()``), so every escape vector
   below must be refused with a specific reason code, while the real templates
   keep working.
2. ``evaluate_compiled_factor`` / ``evaluate_factor_extended`` — a rejected
   candidate still carries a runnable ``compiled_code``, so evaluation must
   refuse it rather than trusting every call site to check ``valid``.
3. ``compile_factor_extended`` — the extended-set reclassification used a subset
   test against possibly-empty sets, which also flipped complexity-limit
   rejections into ``valid: True``.

The DSL-level rejections in ``factor_candidate_compiler._validate_node`` had no
test coverage at all before this file.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from akshare_mcp.services.factor_candidate_compiler import (
    compile_factor_candidate,
    evaluate_compiled_factor,
)
from akshare_mcp.services.pandas_formula_guard import (
    FormulaRejected,
    compile_pandas_formula,
)

COLUMNS = frozenset({"close", "open", "high", "low", "volume", "amount", "market_cap"})

# The exact formula templates llm_alpha._build_local_candidate_pool emits.
REAL_TEMPLATES = [
    "(close.pct_change(60) - close.pct_change(20))",
    "(close.pct_change(20)) / (close.pct_change().rolling(20).std() + 1e-9)",
    "-(close.pct_change(5))",
    "(close - close.rolling(20).mean()) / (close.rolling(20).std() + 1e-9)",
    "close.pct_change(20) * (volume / (volume.rolling(20).mean() + 1e-9))",
    "close.pct_change(5) - volume.pct_change(5)",
    "((high - low) / (close + 1e-9)).rolling(10).mean()",
    "(open / (close.shift(1) + 1e-9) - 1) * close.pct_change(1)",
    "(amount / (market_cap + 1e-9)).rolling(10).mean()",
    "((volume / (volume.rolling(5).mean() + 1e-9)) - 1) * close.pct_change(3)",
]


@pytest.mark.parametrize("formula", REAL_TEMPLATES)
def test_real_templates_still_compile(formula: str) -> None:
    """Hardening must not silently disable the feature it protects."""
    assert compile_pandas_formula(formula, allowed_names=COLUMNS) is not None


@pytest.mark.parametrize(
    ("formula", "expected_code"),
    [
        # Attribute-chain escapes: the reason emptying __builtins__ is not enough.
        ("().__class__.__base__.__subclasses__()", "private_attribute"),
        ("close.__class__", "private_attribute"),
        ("close.rolling(20).mean().__reduce__()", "private_attribute"),
        ("close.__dict__", "private_attribute"),
        # Imports and arbitrary calls.
        ('__import__("os").system("echo pwned")', "unsupported_attribute"),
        ('open("/etc/passwd")', "unsupported_function"),
        ("eval('1')", "unsupported_function"),
        ("close.apply(print)", "unsupported_attribute"),
        ("close.to_csv('/tmp/x')", "unsupported_attribute"),
        # Constructs outside the expression dialect.
        ("[x for x in close]", "unsupported_node"),
        ("{k: 1 for k in close}", "unsupported_node"),
        ("close[0]", "unsupported_node"),
        ("close > open", "unsupported_node"),
        ("close if close else open", "unsupported_node"),
        ("(lambda: 1)()", "unsupported_call"),
        ("close | open", "unsupported_operator"),
        ("close % open", "unsupported_operator"),
        # `not` parses as UnaryOp(Not), so it is caught by the operator check.
        ("not close", "unsupported_operator"),
        # Data-shape rejections.
        ("unknown_col.pct_change(5)", "unknown_name"),
        ('close.pct_change("evil")', "non_numeric_constant"),
        ("close.pct_change(True)", "non_numeric_constant"),
        # Assignment / multi-statement forms.
        ("text_signal_score=0.5; volume_confirm=1", "syntax_error"),
        ("import os", "syntax_error"),
        ("", "empty"),
    ],
)
def test_malicious_formulas_are_refused_with_a_reason(formula: str, expected_code: str) -> None:
    with pytest.raises(FormulaRejected) as excinfo:
        compile_pandas_formula(formula, allowed_names=COLUMNS)
    assert excinfo.value.code == expected_code


def test_refusals_are_distinguishable_from_other_failures() -> None:
    """A bare None told callers nothing; the reason code must be specific."""
    with pytest.raises(FormulaRejected) as escape:
        compile_pandas_formula("().__class__", allowed_names=COLUMNS)
    with pytest.raises(FormulaRejected) as unknown:
        compile_pandas_formula("nope", allowed_names=COLUMNS)
    assert escape.value.code != unknown.value.code


def test_length_limit_applies() -> None:
    with pytest.raises(FormulaRejected) as too_long:
        compile_pandas_formula("close + " * 200 + "close", allowed_names=COLUMNS)
    assert too_long.value.code == "too_long"


def test_node_and_depth_limits_apply() -> None:
    # Grouping parentheses do not create AST nodes, so depth has to come from
    # real nesting. Chained method calls nest one level per call.
    deep = "close" + ".rolling(20).mean()" * 12
    assert len(deep) <= 600, "precondition: must fail on depth, not length"
    with pytest.raises(FormulaRejected) as excinfo:
        compile_pandas_formula(deep, allowed_names=COLUMNS)
    assert excinfo.value.code in {"too_deep", "too_complex"}


def test_redundant_parentheses_are_not_treated_as_depth() -> None:
    """Parens are free in the AST; a legitimate wrapped expression must pass."""
    assert compile_pandas_formula("(((close.pct_change(5))))", allowed_names=COLUMNS) is not None


def _frame(rows: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    close = pd.Series(100 + np.cumsum(rng.standard_normal(rows)))
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=rows, freq="D"),
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": rng.integers(100_000, 900_000, rows).astype(float),
            "amount": rng.random(rows) * 1e7,
        }
    )


def _candidate(expression: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "probe_factor",
        "hypothesis": "probe",
        "family": "momentum",
        "inputs": ["close"],
        "expression_dsl": expression,
        "expected_holding_period": 10,
        "expected_regime": [],
        "complexity_hint": "medium",
        "novelty_rationale": "probe",
    }
    payload.update(overrides)
    return payload


def test_valid_dsl_candidate_still_evaluates() -> None:
    compiled = compile_factor_candidate(_candidate("ts_mean(close, 20) - close"))
    assert compiled["valid"] is True
    series = evaluate_compiled_factor(compiled, _frame())
    assert isinstance(series, pd.Series)
    assert series.notna().any()


def test_invalid_candidate_cannot_be_evaluated() -> None:
    """A rejected candidate still carries compiled_code; it must not run."""
    compiled = compile_factor_candidate(_candidate("ts_mean(unknown_field, 20)"))
    assert compiled["valid"] is False
    assert compiled["compiled_code"] is not None, "precondition: the code object still exists"
    with pytest.raises(ValueError, match="refusing to evaluate an invalid compiled candidate"):
        evaluate_compiled_factor(compiled, _frame())


@pytest.mark.parametrize(
    ("expression", "match"),
    [
        # Method chains are the pandas dialect, not the DSL: node.func is an
        # Attribute, so the Call branch refuses it first.
        ("close.pct_change(5)", "only simple function names are allowed"),
        ("close[0]", "unsupported AST node"),
        ("close > open", "unsupported AST node"),
        ("close % open", "unsupported binary operator"),
        ("close ** 2", "unsupported binary operator"),
        ("~close", "unsupported unary operator"),
        ("(lambda: 1)()", "only simple function names are allowed"),
        ('ts_mean("close", 20)', "only numeric constants are allowed"),
    ],
)
def test_dsl_compiler_rejects_out_of_dialect_expressions(expression: str, match: str) -> None:
    """These _validate_node branches had zero coverage before."""
    with pytest.raises(ValueError, match=match):
        compile_factor_candidate(_candidate(expression))


def test_dsl_compiler_flags_unknown_names_without_executing() -> None:
    compiled = compile_factor_candidate(_candidate("ts_mean(close, 20) + bogus_field"))
    assert compiled["valid"] is False
    assert "bogus_field" in compiled["unsupported_fields"]


def test_extended_compiler_does_not_launder_complexity_rejections() -> None:
    """Empty-set subset checks used to flip a complexity rejection into valid."""
    from akshare_mcp.services.factor_mining_factory.sandbox.compiler import (
        MAX_COMPLEXITY_SCORE,
        compile_factor_extended,
        evaluate_factor_extended,
    )

    # Long but entirely core-vocabulary: the only reason to reject is complexity,
    # so both unsupported sets are empty.
    expression = " + ".join(["ts_mean(close, 20) - ts_std(close, 20)"] * 12)
    compiled = compile_factor_extended(_candidate(expression))
    score = int(compiled["complexity"]["score"])
    assert score > 80, "precondition: expression must exceed the core complexity ceiling"
    assert not compiled["unsupported_fields"]
    assert not compiled["unsupported_functions"]
    if score > MAX_COMPLEXITY_SCORE:
        assert compiled["valid"] is False, "complexity rejection must not be reclassified as valid"
        with pytest.raises(ValueError, match="refusing to evaluate an invalid compiled candidate"):
            evaluate_factor_extended(compiled, _frame())


def test_extended_compiler_still_accepts_extended_vocabulary() -> None:
    from akshare_mcp.services.factor_mining_factory.sandbox.compiler import compile_factor_extended

    compiled = compile_factor_extended(_candidate("ts_max(close, 20) - ts_min(close, 20)"))
    assert compiled["valid"] is True
    assert compiled.get("extended_mode") is True
