#define Py_BUILD_CORE

#include "Python.h"

#include "pycore_frame.h"

// Stuff that will be patched at "JIT time":
extern int _justin_continue(PyThreadState *tstate, _PyInterpreterFrame *frame,
                            PyObject **stack_pointer);

int
_justin_entry(void)
{
    PyThreadState *tstate = PyThreadState_GET();
    _PyInterpreterFrame *tracer = _PyThreadState_GetFrame(tstate);
    _PyInterpreterFrame *frame = _PyFrame_GetFirstComplete(tracer->previous);
    PyObject **stack_pointer = _PyFrame_GetStackPointer(frame);
    return _justin_continue(tstate, frame, stack_pointer);
}
