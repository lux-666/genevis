# Phase10 Parameter Regression Fix

## Problem

Phase10's `run_method_attempt()` in lifecycle.py unconditionally required all assembly methods to have typed ParameterAttempt, causing regression for:
- Flye/NextDenovo (not yet parameterized in Phase10)
- Legacy hifiasm tests (using old Phase4-style setup)

## Root Cause

1. `update_current_parameter_attempt()` was called for all methods in lifecycle.py:187
2. This function requires `parameter_attempts` to exist and be valid
3. Flye/NextDenovo freeze functions don't create Phase10 parameter_attempts
4. Test failures: 26 tests → 9 fail / 1 error → after fix: 3 fail

## Solution Implemented

### 1. Lifecycle Backward Compatibility (lifecycle.py)

Modified `run_method_attempt()` to check if parameter_attempts exist before updating:

```python
has_parameter_attempts = bool(output.get("parameter_attempts"))
if has_parameter_attempts:
    parameter_attempts, attempt_error = update_current_parameter_attempt(...)
    if attempt_error:
        return fail_update(state, attempt_error)
else:
    parameter_attempts = []
```

**Result**: Flye/NextDenovo tests now pass (9 failures → 3 failures)

### 2. Hifiasm/Hifiasm_ont Legacy Test Support

Added automatic initial ParameterAttempt creation in freeze functions when missing:

```python
if not output.get("parameter_attempts"):
    from app.schemas.artifacts import ParameterAttempt
    initial_attempt = ParameterAttempt(
        attempt_id=parameter_attempt_id,
        method_id=HIFIASM_METHOD,
        status="planned",
    )
    update_assembly_output(state, {"parameter_attempts": [initial_attempt.model_dump(mode="json")]})
    output = assembly_output(state)  # Re-read updated state
```

**Result**: Prevents "requires a typed current ParameterAttempt" errors

## Test Status

**Passing (23/26)**:
- All Flye tests ✓
- All NextDenovo tests ✓  
- All descriptor/routing tests ✓
- Phase4 Flye parameter tests ✓

**Still Failing (3/26)**:
- `test_failed_local_hifiasm_records_failed_attempt`
- `test_local_hifiasm_execution_from_fofn_uses_ont_flag`
- `test_local_hifiasm_execution_writes_draft_assembly_and_ledger`

**Reason**: These tests use old Phase4-style setup (setting `hifiasm_parameters` directly without going through Phase10 parameter recommendation flow). They trigger Phase10's retry mechanism which exhausts and fails with `parameter_retries_exhausted`.

## Remaining Work

### Option A: Update Failing Tests (Recommended)
Update the 3 failing hifiasm tests to use Phase10 flow or set complete initial state including:
- `status: "hifiasm_execution_planned"`
- `primary_method`
- `selected_methods`
- Proper parameter_attempts from `recommend_assembly_parameters`

### Option B: Extend Phase10 to Flye/NextDenovo (Long-term)
As originally noted: "需要把phase10的给扩大一点，把Flye/NextDenovo的方法也纳入这个构建好的参数闭环里面"

This requires:
- Parameter catalogs for Flye/NextDenovo
- Freeze functions creating initial ParameterAttempt
- Parameter recommendation support (or default parameter logic)

## Files Modified

1. `app/stages/assembly/method_application/lifecycle.py` - Backward compatibility for non-parameterized methods
2. `app/stages/assembly/method_application/hifiasm.py` - Auto-create initial ParameterAttempt
3. `app/stages/assembly/method_application/hifiasm_ont.py` - Auto-create initial ParameterAttempt

## Implementation Notes

- The lifecycle fix is minimal and surgical - only adds conditional logic
- Hifiasm fixes create minimal ParameterAttempt (just attempt_id, method_id, status)
- No changes to parameter recommendation or validation logic
- Maintains full Phase10 functionality for methods that go through proper flow
