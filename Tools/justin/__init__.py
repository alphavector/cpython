"""The Justin(time) template JIT for CPython 3.12, based on copy-and-patch."""

import ctypes
import dis
import sys
import types
import typing

VERBOSE = True
WARMUP = 1 << 1

_co_code_adaptive = {}
_traces = {}
_lines = {}
_warmups = {}

INSTRUMENTED_JUMP_BACKWARD = dis._all_opmap["INSTRUMENTED_JUMP_BACKWARD"]
JUMP_BACKWARD = dis._all_opmap["JUMP_BACKWARD"]
JUMP_BACKWARD_INTO_TRACE = dis._all_opmap["JUMP_BACKWARD_INTO_TRACE"]

_py_codeunit_p = ctypes.POINTER(ctypes.c_uint16)

def _stderr(*args):
    if VERBOSE:
        print(*args, file=sys.stderr, flush=True)

def _format_range(code: types.CodeType, i: int, j: int):
    if code not in _lines:
        _lines[code] = [lineno for lineno, _, _, _ in code.co_positions()]
    lines = list(filter(None, _lines[code][i // 2: j // 2]))
    lo = min(lines)
    hi = max(lines)
    return f"{code.co_filename}:{lo}-{hi}"

def _trace_jump(code: types.CodeType, i: int, j: int):
    if j <= i:
        key = (code, i)
        warmups = _warmups[key] = _warmups.get(key, 0) + 1
        if warmups <= WARMUP:
            _stderr(f"JUSTIN: - Warming up {_format_range(code, j, i)} ({warmups}/{WARMUP}).") 
            return
        _co_code_adaptive[code] = bytearray(code._co_code_adaptive)
        sys.monitoring.set_local_events(
            sys.monitoring.OPTIMIZER_ID,
            code,
            sys.monitoring.events.INSTRUCTION | sys.monitoring.events.JUMP,
        )
        if code in _traces:
            _stderr(f"JUSTIN:   - Found inner loop!") 
        _traces[code] = i, []
        _stderr(f"JUSTIN: - Recording loop at {_format_range(code, j, i)}:")
    return sys.monitoring.DISABLE

def _trace_instruction(code: types.CodeType, i: int):
    jump, trace = _traces[code]
    trace.append(i)
    if i == jump:
        _compile(code, _co_code_adaptive[code], trace)
        sys.monitoring.set_local_events(
            sys.monitoring.OPTIMIZER_ID, code, sys.monitoring.events.JUMP
        )
        _stderr("JUSTIN:   - Done!")
        del _traces[code]
    return sys.monitoring.DISABLE

def trace(f):
    sys.monitoring.set_local_events(
        sys.monitoring.OPTIMIZER_ID, f.__code__, sys.monitoring.events.JUMP
    )
    return f

sys.monitoring.use_tool_id(sys.monitoring.OPTIMIZER_ID, "Justin")
sys.monitoring.register_callback(
    sys.monitoring.OPTIMIZER_ID,
    sys.monitoring.events.INSTRUCTION,
    _trace_instruction,
)
sys.monitoring.register_callback(
    sys.monitoring.OPTIMIZER_ID, sys.monitoring.events.JUMP, _trace_jump
)

_OFFSETOF_CO_CODE_ADAPTIVE = 192

def _compile(code, co_code_adaptive, traced):
    traced = _remove_superinstructions(co_code_adaptive, traced)
    j = traced[-1]
    c_traced_type = _py_codeunit_p * len(traced)
    c_traced = c_traced_type()
    first_instr = id(code) + _OFFSETOF_CO_CODE_ADAPTIVE
    buff = ctypes.cast(first_instr, _py_codeunit_p)
    ctypes.memmove(
        buff,
        (ctypes.c_uint16 * (len(co_code_adaptive) // 2)).from_buffer(co_code_adaptive),
        len(co_code_adaptive),
    )
    c_traced[:] = [ctypes.cast(first_instr + i, _py_codeunit_p) for i in traced]
    jump = ctypes.cast(ctypes.c_void_p(first_instr + j), ctypes.POINTER(ctypes.c_uint8))
    assert jump.contents.value == INSTRUMENTED_JUMP_BACKWARD
    jump.contents.value = JUMP_BACKWARD
    compile_trace = ctypes.pythonapi._PyJIT_CompileTrace
    compile_trace.argtypes = (ctypes.c_int, c_traced_type)
    compile_trace.restype = ctypes.POINTER(ctypes.c_ubyte)
    buffer = ctypes.cast(compile_trace(len(traced), c_traced), ctypes.c_void_p)
    if buffer.value is None:
        return False
    jump.contents.value = JUMP_BACKWARD_INTO_TRACE
    cache = ctypes.c_void_p(first_instr + j + 4)
    ctypes.cast(cache, ctypes.POINTER(ctypes.c_uint64)).contents.value = buffer.value
    return True


def _remove_superinstructions(co_code_adaptive: bytearray, trace: typing.Iterable[int]):
    out = []
    t = iter(trace)
    for i in t:
        out.append(i)
        if "__" in dis._all_opname[co_code_adaptive[i]]:
            next(t)
    return out
