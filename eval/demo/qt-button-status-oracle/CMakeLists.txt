cmake_minimum_required(VERSION 3.24)
project(ForgePilotButtonStatusOracle LANGUAGES CXX)

if(NOT IS_DIRECTORY "${FORGEPILOT_WORKTREE}" OR
   NOT EXISTS "${FORGEPILOT_WORKTREE}/CMakeLists.txt")
    message(FATAL_ERROR "A button status worktree is required")
endif()
file(SHA256 "${FORGEPILOT_WORKTREE}/CMakeLists.txt" selected_hash)
if(NOT selected_hash STREQUAL "c1fdadb7c63ef4316e11adf9fbea49b8f15eeadddf7337f223b87dad71a68f1c")
    message(FATAL_ERROR "Oracle is bound to the button status demo only")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_AUTOMOC ON)
include(QtKit.cmake OPTIONAL)
find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets Test)
add_library(protected_demo_ui STATIC
    "${FORGEPILOT_WORKTREE}/src/StatusController.cpp"
    "${FORGEPILOT_WORKTREE}/src/StatusController.h"
    "${FORGEPILOT_WORKTREE}/src/MainWindow.cpp"
    "${FORGEPILOT_WORKTREE}/src/MainWindow.h")
target_include_directories(protected_demo_ui PUBLIC "${FORGEPILOT_WORKTREE}/src")
target_link_libraries(protected_demo_ui PUBLIC Qt6::Widgets)
add_executable(ProtectedButtonStatus protected_button_status.cpp)
target_link_libraries(ProtectedButtonStatus PRIVATE protected_demo_ui Qt6::Test)
enable_testing()
add_test(NAME protected_button_status COMMAND ProtectedButtonStatus)
set_tests_properties(protected_button_status PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
foreach(module Core Gui Widgets Test)
    add_custom_command(TARGET ProtectedButtonStatus POST_BUILD COMMAND ${CMAKE_COMMAND} -E copy_if_different
        $<TARGET_FILE:Qt6::${module}> $<TARGET_FILE_DIR:ProtectedButtonStatus>)
endforeach()
add_custom_command(TARGET ProtectedButtonStatus POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory $<TARGET_FILE_DIR:ProtectedButtonStatus>/platforms
    COMMAND ${CMAKE_COMMAND} -E copy_if_different $<TARGET_FILE:Qt6::QOffscreenIntegrationPlugin>
        $<TARGET_FILE_DIR:ProtectedButtonStatus>/platforms)
