cmake_minimum_required(VERSION 3.24)
project(ForgePilotButtonDemo LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
include(cmake/QtKit.cmake OPTIONAL)
find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets Test)
add_library(demo_ui STATIC src/StatusController.cpp src/StatusController.h src/MainWindow.cpp src/MainWindow.h)
target_include_directories(demo_ui PUBLIC src)
target_link_libraries(demo_ui PUBLIC Qt6::Widgets)
add_executable(ButtonStatusDemo src/main.cpp)
target_link_libraries(ButtonStatusDemo PRIVATE demo_ui)
add_executable(ButtonStatusTests tests/tst_button_status.cpp)
target_link_libraries(ButtonStatusTests PRIVATE demo_ui Qt6::Test)
enable_testing()
add_test(NAME button_updates_status COMMAND ${CMAKE_COMMAND}
    -DTEST_EXE=$<TARGET_FILE:ButtonStatusTests>
    -DREPORT=${CMAKE_CURRENT_BINARY_DIR}/button-status-results.txt
    -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/RunQtTest.cmake)
set_tests_properties(button_updates_status PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 15)
foreach(target ButtonStatusDemo ButtonStatusTests)
    foreach(module Core Gui Widgets)
        add_custom_command(TARGET ${target} POST_BUILD COMMAND ${CMAKE_COMMAND} -E copy_if_different $<TARGET_FILE:Qt6::${module}> $<TARGET_FILE_DIR:${target}>)
    endforeach()
    add_custom_command(TARGET ${target} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E make_directory $<TARGET_FILE_DIR:${target}>/platforms
        COMMAND ${CMAKE_COMMAND} -E copy_if_different $<TARGET_FILE:Qt6::QWindowsIntegrationPlugin> $<TARGET_FILE_DIR:${target}>/platforms
        COMMAND ${CMAKE_COMMAND} -E copy_if_different $<TARGET_FILE:Qt6::QOffscreenIntegrationPlugin> $<TARGET_FILE_DIR:${target}>/platforms)
endforeach()
add_custom_command(TARGET ButtonStatusTests POST_BUILD COMMAND ${CMAKE_COMMAND} -E copy_if_different $<TARGET_FILE:Qt6::Test> $<TARGET_FILE_DIR:ButtonStatusTests>)
