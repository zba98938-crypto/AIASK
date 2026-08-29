"""Data validation adapter.

Provides ``DataValidationAdapter`` interface and two implementations:

1. ``BuiltinValidationAdapter`` — uses existing ``data_quality.py`` logic
   to validate datasets, create checkpoints, and produce validation results.
2. ``GreatExpectationsAdapter`` — wraps GX library when installed.

Usage::

    adapter = get_data_validation_adapter()
    result = adapter.validate_dataset(
        records=[{"close": 10.5, "volume": 1000}],
        expectations={"required_fields": ["close", "volume"]},
    )
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


# ── Result dataclass ──────────────────────────────────────────────────────────

class ValidationResult:
    """Result of a data validation run."""

    def __init__(
        self,
        passed: bool,
        stats: dict[str, Any],
        *,
        validation_id: str = "",
        method: str = "builtin",
        backend: str = "builtin",
        expectations_evaluated: int = 0,
        expectations_passed: int = 0,
        details: list[dict[str, Any]] | None = None,
        backend_requested: str = "builtin",
        backend_used: str = "builtin",
        fallback_used: bool = False,
        fallback_reason: str | None = None,
    ) -> None:
        self.passed = passed
        self.stats = stats
        self.validation_id = validation_id or f"val-{uuid4().hex[:12]}"
        self.method = method
        self.backend = backend
        self.expectations_evaluated = expectations_evaluated
        self.expectations_passed = expectations_passed
        self.details = details or []
        self.backend_requested = backend_requested
        self.backend_used = backend_used
        self.fallback_used = fallback_used
        self.fallback_reason = fallback_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "validation_id": self.validation_id,
            "passed": self.passed,
            "method": self.method,
            "backend": self.backend,
            "expectations_evaluated": self.expectations_evaluated,
            "expectations_passed": self.expectations_passed,
            "backend_requested": self.backend_requested,
            "backend_used": self.backend_used,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "stats": self.stats,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ValidationResult":
        return cls(
            passed=bool(payload.get("passed")),
            stats=dict(payload.get("stats") or {}),
            validation_id=str(payload.get("validation_id") or ""),
            method=str(payload.get("method") or "builtin"),
            backend=str(payload.get("backend") or "builtin"),
            expectations_evaluated=int(payload.get("expectations_evaluated") or 0),
            expectations_passed=int(payload.get("expectations_passed") or 0),
            details=list(payload.get("details") or []),
            backend_requested=str(payload.get("backend_requested") or payload.get("backend") or "builtin"),
            backend_used=str(payload.get("backend_used") or payload.get("backend") or "builtin"),
            fallback_used=bool(payload.get("fallback_used", False)),
            fallback_reason=str(payload.get("fallback_reason") or "") or None,
        )


# ── Abstract interface ────────────────────────────────────────────────────────

class DataValidationAdapter(ABC):
    """Interface for data validation adapters."""

    @abstractmethod
    def validate_dataset(
        self,
        records: list[dict[str, Any]],
        expectations: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """Validate a dataset against expectations."""
        ...

    @abstractmethod
    def create_checkpoint(
        self,
        checkpoint_name: str,
        validation_results: list[ValidationResult],
    ) -> dict[str, Any]:
        """Create a checkpoint from multiple validation results."""
        ...

    @abstractmethod
    def backend_name(self) -> str:
        ...


# ── Builtin implementation ────────────────────────────────────────────────────

class BuiltinValidationAdapter(DataValidationAdapter):
    """Pure-Python dataset validation using conventions from data_quality.py.

    Supports expectations:
    - required_fields: list of field names that must be present
    - min_record_count: minimum number of records
    - max_null_ratio: maximum allowed null ratio per field
    - field_types: mapping of field -> expected python type name
    - allowed_values: mapping of field -> allowed value list
    - min_quality_threshold: minimum quality score
    """

    def validate_dataset(
        self,
        records: list[dict[str, Any]],
        expectations: dict[str, Any] | None = None,
    ) -> ValidationResult:
        exp = dict(expectations or {})
        details: list[dict[str, Any]] = []
        total_checks = 0
        passed_checks = 0

        # Check 1: min_record_count
        min_count = int(exp.get("min_record_count", 0))
        if min_count > 0:
            total_checks += 1
            ok = len(records) >= min_count
            if ok:
                passed_checks += 1
            details.append({
                "expectation": "min_record_count",
                "passed": ok,
                "expected": min_count,
                "actual": len(records),
            })

        # Check 2: required_fields
        required = list(exp.get("required_fields", []))
        if required and records:
            total_checks += 1
            sample = records[0]
            missing = [f for f in required if f not in sample]
            ok = len(missing) == 0
            if ok:
                passed_checks += 1
            details.append({
                "expectation": "required_fields",
                "passed": ok,
                "expected": required,
                "missing": missing,
            })

        # Check 2b: non_null_fields — F-N43-4 fix (诊断报告 §N43):
        # 历史问题: validate_dataset 仅查列存在(expect_column_to_exist)，从不校验单元格 null，
        # 导致含 null 的脏数据 passed=true/quality_score=1.0，与字段级 accepted_ratio 互斥。
        # 修复: non_null_fields 真正逐行校验内容非空，使两套质量结论一致。
        non_null_fields = [
            str(item).strip()
            for item in list(exp.get("non_null_fields") or [])
            if str(item).strip()
        ]
        for fld in non_null_fields:
            total_checks += 1
            null_count = sum(
                1 for r in records
                if not isinstance(r, dict) or r.get(fld) is None or r.get(fld) == ""
            )
            ok = bool(records) and null_count == 0
            if ok:
                passed_checks += 1
            details.append({
                "expectation": f"non_null_fields:{fld}",
                "passed": ok,
                "null_or_missing_count": null_count,
                "total_count": len(records),
            })

        # Check 3: max_null_ratio (per field)
        max_null = float(exp.get("max_null_ratio", 1.0))
        if records and max_null < 1.0:
            all_fields = set()
            for r in records:
                all_fields.update(r.keys())
            for fld in sorted(all_fields):
                total_checks += 1
                null_count = sum(1 for r in records if r.get(fld) is None)
                null_ratio = null_count / max(len(records), 1)
                ok = null_ratio <= max_null
                if ok:
                    passed_checks += 1
                details.append({
                    "expectation": f"max_null_ratio:{fld}",
                    "passed": ok,
                    "threshold": max_null,
                    "actual_ratio": round(null_ratio, 4),
                    "null_count": null_count,
                    "total_count": len(records),
                })

        field_types = dict(exp.get("field_types") or {})
        for fld, expected_type in field_types.items():
            total_checks += 1
            normalized = str(expected_type or "").strip().lower()
            values = [r.get(fld) for r in records if fld in r and r.get(fld) is not None]
            type_map = {
                "int": int,
                "integer": int,
                "float": float,
                "number": (int, float),
                "str": str,
                "string": str,
                "bool": bool,
                "boolean": bool,
            }
            expected_cls = type_map.get(normalized)
            ok = bool(expected_cls) and all(isinstance(value, expected_cls) for value in values)
            if ok:
                passed_checks += 1
            details.append({
                "expectation": f"field_types:{fld}",
                "passed": ok,
                "expected": normalized,
                "checked_count": len(values),
            })

        allowed_values = dict(exp.get("allowed_values") or {})
        for fld, allowed in allowed_values.items():
            total_checks += 1
            allowed_set = {item for item in list(allowed or [])}
            values = [r.get(fld) for r in records if fld in r and r.get(fld) is not None]
            invalid = [value for value in values if value not in allowed_set]
            ok = len(invalid) == 0
            if ok:
                passed_checks += 1
            details.append({
                "expectation": f"allowed_values:{fld}",
                "passed": ok,
                "allowed_count": len(allowed_set),
                "invalid_count": len(invalid),
                "invalid_samples": invalid[:5],
            })

        # Overall stats
        if total_checks == 0:
            total_checks = 1
            passed_checks = 1
            details.append({
                "expectation": "no_expectations_defined",
                "passed": True,
                "note": "No expectations provided; trivially passed.",
            })

        quality_score = round(passed_checks / max(total_checks, 1), 4)
        min_threshold = float(exp.get("min_quality_threshold", 0.95))
        overall_passed = quality_score >= min_threshold

        return ValidationResult(
            passed=overall_passed,
            stats={
                "record_count": len(records),
                "quality_score": quality_score,
                "min_quality_threshold": min_threshold,
            },
            method="builtin_expectations",
            backend="builtin",
            expectations_evaluated=total_checks,
            expectations_passed=passed_checks,
            details=details,
            backend_requested="builtin",
            backend_used="builtin",
        )

    def create_checkpoint(
        self,
        checkpoint_name: str,
        validation_results: list[ValidationResult],
    ) -> dict[str, Any]:
        all_passed = all(r.passed for r in validation_results)
        return {
            "checkpoint_name": checkpoint_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "backend": "builtin",
            "backend_requested": "builtin",
            "backend_used": "builtin",
            "fallback_used": False,
            "fallback_reason": None,
            "all_passed": all_passed,
            "validation_count": len(validation_results),
            "passed_count": sum(1 for r in validation_results if r.passed),
            "failed_count": sum(1 for r in validation_results if not r.passed),
            "actions": [] if all_passed else ["raise_data_quality_alert"],
            "validations": [r.to_dict() for r in validation_results],
        }

    def backend_name(self) -> str:
        return "builtin"


# ── Great Expectations adapter (optional) ─────────────────────────────────────

class GreatExpectationsAdapter(DataValidationAdapter):
    """Wraps Great Expectations library when installed.

    Falls back to BuiltinValidationAdapter if GX is not available.
    """

    def __init__(self) -> None:
        self._available = False
        self._fallback = BuiltinValidationAdapter()
        self._gx = None
        self._pd = None
        try:
            import great_expectations as gx
            import pandas as pd

            self._available = True
            self._gx = gx
            self._pd = pd
        except ImportError:
            pass

    @staticmethod
    def _type_alias(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return {
            "int": "int64",
            "integer": "int64",
            "float": "float64",
            "number": "float64",
            "str": "str",
            "string": "str",
            "bool": "bool",
            "boolean": "bool",
        }.get(normalized, normalized)

    def _build_expectation_suite(
        self,
        expectations: dict[str, Any] | None,
        *,
        records: list[dict[str, Any]],
    ):
        assert self._gx is not None
        from great_expectations.core.expectation_suite import ExpectationSuite
        from great_expectations.expectations.expectation_configuration import (
            ExpectationConfiguration,
        )

        exp = dict(expectations or {})
        suite_expectations: list[Any] = []

        min_count = int(exp.get("min_record_count", 0) or 0)
        if min_count > 0:
            suite_expectations.append(
                ExpectationConfiguration(
                    type="expect_table_row_count_to_be_between",
                    kwargs={"min_value": min_count},
                )
            )

        required_fields = [str(item).strip() for item in list(exp.get("required_fields") or []) if str(item).strip()]
        for field in required_fields:
            suite_expectations.append(
                ExpectationConfiguration(
                    type="expect_column_to_exist",
                    kwargs={"column": field},
                )
            )

        # F-N43-4 fix (诊断报告 §N43): non_null_fields 必须生成真正的「值非空」期望，
        # 否则 GX 仅查列存在，含 null 的脏数据仍 passed=true（与字段级结论互斥）。
        non_null_fields = [
            str(item).strip()
            for item in list(exp.get("non_null_fields") or [])
            if str(item).strip()
        ]
        for field in non_null_fields:
            suite_expectations.append(
                ExpectationConfiguration(
                    type="expect_column_values_to_not_be_null",
                    kwargs={"column": field},
                )
            )

        max_null_ratio = float(exp.get("max_null_ratio", 1.0) or 1.0)
        if max_null_ratio < 1.0:
            candidate_fields = set(required_fields)
            for row in records:
                if isinstance(row, dict):
                    candidate_fields.update(str(key) for key in row.keys())
            min_non_null_ratio = max(0.0, min(1.0, 1.0 - max_null_ratio))
            for field in sorted(candidate_fields):
                suite_expectations.append(
                    ExpectationConfiguration(
                        type="expect_column_proportion_of_non_null_values_to_be_between",
                        kwargs={"column": field, "min_value": min_non_null_ratio},
                    )
                )

        field_types = dict(exp.get("field_types") or {})
        for field, expected_type in field_types.items():
            suite_expectations.append(
                ExpectationConfiguration(
                    type="expect_column_values_to_be_of_type",
                    kwargs={"column": str(field), "type_": self._type_alias(expected_type)},
                )
            )

        allowed_values = dict(exp.get("allowed_values") or {})
        for field, allowed in allowed_values.items():
            suite_expectations.append(
                ExpectationConfiguration(
                    type="expect_column_values_to_be_in_set",
                    kwargs={"column": str(field), "value_set": list(allowed or [])},
                )
            )

        return ExpectationSuite(
            name=f"runtime_suite_{uuid4().hex[:12]}",
            expectations=suite_expectations,
            meta={"source": "aiask_runtime_data_validation"},
        )

    def _build_ephemeral_context(self):
        assert self._gx is not None
        try:
            from great_expectations.data_context.types.base import (
                DataContextConfig,
                InMemoryStoreBackendDefaults,
            )

            config = DataContextConfig(
                store_backend_defaults=InMemoryStoreBackendDefaults(init_temp_docs_sites=False)
            )
            return self._gx.get_context(mode="ephemeral", project_config=config)
        except Exception:
            return self._gx.get_context(mode="ephemeral")

    def validate_dataset(
        self,
        records: list[dict[str, Any]],
        expectations: dict[str, Any] | None = None,
    ) -> ValidationResult:
        if not self._available:
            result = self._fallback.validate_dataset(records, expectations)
            result.backend = "builtin"
            result.backend_requested = "great_expectations"
            result.backend_used = "builtin"
            result.fallback_used = True
            result.fallback_reason = "great_expectations_not_installed"
            return result

        assert self._gx is not None
        assert self._pd is not None

        try:
            frame = self._pd.DataFrame(list(records or []))
            context = self._build_ephemeral_context()
            datasource = context.data_sources.add_pandas(f"runtime_ds_{uuid4().hex[:10]}")
            batch = datasource.read_dataframe(
                frame,
                asset_name=f"runtime_asset_{uuid4().hex[:10]}",
                batch_metadata={"validation_id": f"val-{uuid4().hex[:12]}"},
            )
            suite = self._build_expectation_suite(expectations, records=list(records or []))
            validation = batch.validate(expect=suite, result_format="SUMMARY")
            payload = validation.to_json_dict()

            results = list(payload.get("results") or [])
            details: list[dict[str, Any]] = []
            passed_checks = 0
            for item in results:
                row = dict(item or {})
                success = bool(row.get("success"))
                if success:
                    passed_checks += 1
                expectation_config = dict(row.get("expectation_config") or {})
                kwargs = dict(expectation_config.get("kwargs") or {})
                result_payload = dict(row.get("result") or {})
                detail = {
                    "expectation": expectation_config.get("type") or "unknown_expectation",
                    "passed": success,
                    "column": kwargs.get("column"),
                    "expected": kwargs,
                    "unexpected_count": result_payload.get("unexpected_count"),
                    "unexpected_percent": result_payload.get("unexpected_percent"),
                    "observed_value": result_payload.get("observed_value"),
                }
                if row.get("exception_info"):
                    detail["exception_info"] = row.get("exception_info")
                details.append(detail)

            stats = {
                "record_count": len(records),
                "quality_score": round(
                    passed_checks / max(len(results), 1),
                    4,
                ),
                "min_quality_threshold": float(dict(expectations or {}).get("min_quality_threshold", 0.95)),
                "gx_statistics": dict(payload.get("statistics") or {}),
                "suite_name": payload.get("suite_name") or suite.name,
            }

            return ValidationResult(
                passed=bool(payload.get("success")),
                stats=stats,
                validation_id=str(payload.get("id") or ""),
                method="great_expectations_runtime",
                backend="great_expectations",
                expectations_evaluated=len(results),
                expectations_passed=passed_checks,
                details=details,
                backend_requested="great_expectations",
                backend_used="great_expectations",
                fallback_used=False,
                fallback_reason=None,
            )
        except Exception as exc:
            result = self._fallback.validate_dataset(records, expectations)
            result.backend = "builtin"
            result.backend_requested = "great_expectations"
            result.backend_used = "builtin"
            result.fallback_used = True
            result.fallback_reason = f"great_expectations_runtime_failed:{type(exc).__name__}"
            result.details.append(
                {
                    "expectation": "great_expectations_runtime",
                    "passed": False,
                    "note": str(exc),
                }
            )
            return result

    def create_checkpoint(
        self,
        checkpoint_name: str,
        validation_results: list[ValidationResult],
    ) -> dict[str, Any]:
        if not self._available:
            cp = self._fallback.create_checkpoint(checkpoint_name, validation_results)
            cp["backend_requested"] = "great_expectations"
            cp["backend_used"] = "builtin"
            cp["fallback_used"] = True
            cp["fallback_reason"] = "great_expectations_not_installed"
            return cp

        all_passed = all(result.passed for result in validation_results)
        return {
            "checkpoint_name": checkpoint_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "backend": "great_expectations",
            "backend_requested": "great_expectations",
            "backend_used": "great_expectations",
            "fallback_used": False,
            "fallback_reason": None,
            "all_passed": all_passed,
            "validation_count": len(validation_results),
            "passed_count": sum(1 for result in validation_results if result.passed),
            "failed_count": sum(1 for result in validation_results if not result.passed),
            "actions": [] if all_passed else ["great_expectations_checkpoint_failed", "raise_data_quality_alert"],
            "validations": [result.to_dict() for result in validation_results],
        }

    def backend_name(self) -> str:
        return "great_expectations" if self._available else "great_expectations_requested_builtin_fallback"


# ── Factory ───────────────────────────────────────────────────────────────────

def get_data_validation_adapter(prefer_gx: bool = True) -> DataValidationAdapter:
    """Get the best available data validation adapter.

    Parameters
    ----------
    prefer_gx:
        If True, try Great Expectations first, fallback to builtin.
    """
    if prefer_gx:
        return GreatExpectationsAdapter()
    return BuiltinValidationAdapter()
