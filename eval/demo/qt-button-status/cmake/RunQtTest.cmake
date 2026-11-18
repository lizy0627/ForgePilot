# A Windows GUI Qt Test may not expose stdout to CTest. Preserve and forward its
# explicit text log so the original Runtime sees the assertion, not only exit 1.
if(NOT DEFINED TEST_EXE OR NOT DEFINED REPORT)
    message(FATAL_ERROR "Missing configured test executable/report")
endif()
file(REMOVE "${REPORT}")
execute_process(COMMAND "${TEST_EXE}" -o "${REPORT},txt"
    RESULT_VARIABLE status TIMEOUT 12)
if(NOT EXISTS "${REPORT}")
    message(FATAL_ERROR "Qt Test did not produce its current report: ${status}")
endif()
file(READ "${REPORT}" detail)
message("${detail}")
if(NOT "${status}" STREQUAL "0")
    message(FATAL_ERROR "Qt Test failed with exit code ${status}")
endif()
