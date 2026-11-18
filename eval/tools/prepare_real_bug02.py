"""Prepare an independent repair candidate for the locked pkg-config issue."""

from pathlib import Path
import os
import shutil

temp = Path(os.environ["TEMP"])
source = temp / "forgepilot-audit-real-bug-02-20261104"
target = temp / "forgepilot-repaired-real-bug-02-20261104"
shutil.copytree(source, target)
cm = target / "cmake/cxxopts.cmake"
text = cm.read_text(encoding="utf-8")
marker = '    configure_file("${PackagingTemplatesDir}/pkgconfig.pc.in" "${PKG_CONFIG_FILE_NAME}" @ONLY)'
assert text.count(marker) == 1
text = text.replace(marker, '''    if(IS_ABSOLUTE "${CMAKE_INSTALL_INCLUDEDIR}")
        set(PKG_CONFIG_INCLUDEDIR "${CMAKE_INSTALL_INCLUDEDIR}")
    else()
        set(PKG_CONFIG_INCLUDEDIR "\\${prefix}/${CMAKE_INSTALL_INCLUDEDIR}")
    endif()
''' + marker)
cm.write_text(text, encoding="utf-8")
pc = target / "packaging/pkgconfig.pc.in"
text = pc.read_text(encoding="utf-8")
assert 'includedir=${prefix}/@CMAKE_INSTALL_INCLUDEDIR@' in text
pc.write_text(text.replace('includedir=${prefix}/@CMAKE_INSTALL_INCLUDEDIR@',
                           'includedir=@PKG_CONFIG_INCLUDEDIR@'), encoding="utf-8")
tests = target / "test/CMakeLists.txt"
with tests.open("a", encoding="utf-8") as out:
    out.write('''
add_test(NAME pkg-config-default
    COMMAND ${CMAKE_COMMAND} -S "${CMAKE_CURRENT_SOURCE_DIR}/pkg-config-test"
        -B "${CMAKE_CURRENT_BINARY_DIR}/pkg-config-default"
        "-DPROJECT_SOURCE=${PROJECT_SOURCE_DIR}" "-DCASE=default")
add_test(NAME pkg-config-relative
    COMMAND ${CMAKE_COMMAND} -S "${CMAKE_CURRENT_SOURCE_DIR}/pkg-config-test"
        -B "${CMAKE_CURRENT_BINARY_DIR}/pkg-config-relative"
        "-DPROJECT_SOURCE=${PROJECT_SOURCE_DIR}" "-DCASE=relative")
add_test(NAME pkg-config-absolute
    COMMAND ${CMAKE_COMMAND} -S "${CMAKE_CURRENT_SOURCE_DIR}/pkg-config-test"
        -B "${CMAKE_CURRENT_BINARY_DIR}/pkg-config-absolute"
        "-DPROJECT_SOURCE=${PROJECT_SOURCE_DIR}" "-DCASE=absolute")
''')
new = target / "test/pkg-config-test/CMakeLists.txt"
new.parent.mkdir()
new.write_text(r'''cmake_minimum_required(VERSION 3.5)
project(PkgConfigPathCheck NONE)
if(NOT PROJECT_SOURCE OR NOT CASE)
    message(FATAL_ERROR "Missing test inputs")
endif()
set(case_build "${CMAKE_CURRENT_BINARY_DIR}/generated")
set(options -DCXXOPTS_BUILD_TESTS=OFF -DCXXOPTS_BUILD_EXAMPLES=OFF)
if(CASE STREQUAL "relative")
    list(APPEND options -DCMAKE_INSTALL_INCLUDEDIR=custom/include)
    set(expected "\${prefix}/custom/include")
elseif(CASE STREQUAL "absolute")
    set(absolute_path "${CMAKE_CURRENT_BINARY_DIR}/absolute-include")
    list(APPEND options "-DCMAKE_INSTALL_INCLUDEDIR=${absolute_path}")
    set(expected "${absolute_path}")
elseif(CASE STREQUAL "default")
    set(expected "\${prefix}/include")
else()
    message(FATAL_ERROR "Unknown case")
endif()
execute_process(COMMAND "${CMAKE_COMMAND}" -S "${PROJECT_SOURCE}" -B "${case_build}"
    ${options} RESULT_VARIABLE status)
if(NOT status EQUAL 0)
    message(FATAL_ERROR "Nested configuration failed")
endif()
file(STRINGS "${case_build}/cxxopts.pc" include_line REGEX "^includedir=")
if(NOT include_line STREQUAL "includedir=${expected}")
    message(FATAL_ERROR "Incorrect pkg-config includedir")
endif()
''', encoding="utf-8")
print(target)
