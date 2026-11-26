/*
 * This file contains Bytedance specifications, including
 * core macro definitions, interface declarations,
 * global variables and other Bytedance headers
 *
 */

#ifndef Py_PYBYTED_H
#define Py_PYBYTED_H

#ifdef BYTEDANCE_PYTHON
    #ifdef PY_MICRO_BYTEDVERSION
        /* Bytedance Version as a string */
        #define BM_SEPARATOR            "bm"
        #define PY_MAJOR_BYTEDVERSION   1
        #define PY_MINOR_BYTEDVERSION   0
        #define PY_BYTEDVERSION         PY_VERSION "." \
                                        BM_SEPARATOR "." \
                                        Py_STRINGIFY(PY_MAJOR_BYTEDVERSION) "." \
                                        Py_STRINGIFY(PY_MINOR_BYTEDVERSION) "." \
                                        Py_STRINGIFY(PY_MICRO_BYTEDVERSION)
        /*--end constants--*/
    #else
        #define PY_BYTEDVERSION     PY_VERSION
    #endif
#endif

#endif /* Py_PYBYTED_H */
