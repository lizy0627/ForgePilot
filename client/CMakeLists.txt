cmake_minimum_required(VERSION 3.21)
project(ForgePilotClient VERSION 0.1.0 LANGUAGES CXX)

if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "MSVC")
    message(FATAL_ERROR "ForgePilot Client requires Microsoft MSVC.")
endif()
if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "ForgePilot Client requires an x64 toolchain.")
endif()

find_package(Qt6 6.8 REQUIRED COMPONENTS Network Quick Qml QuickControls2 QuickDialogs2 Concurrent)
qt_standard_project_setup(REQUIRES 6.8)

add_library(ForgePilotCommunication STATIC
    src/Protocol.h src/Protocol.cpp
    src/JsonHttp.h src/JsonHttp.cpp
    src/AgentApiClient.h src/AgentApiClient.cpp
    src/SseParser.h src/SseParser.cpp
    src/SseClient.h src/SseClient.cpp
    src/ConversationStreamClient.h src/ConversationStreamClient.cpp
    src/TaskController.h src/TaskController.cpp src/TaskPresentation.cpp
    src/ConversationController.h src/ConversationController.cpp
    src/RagController.h src/RagController.cpp
    src/MemoryController.h src/MemoryController.cpp
    src/McpController.h src/McpController.cpp
    src/RecordListModel.h src/RecordListModel.cpp
    src/RepositoryModel.h src/RepositoryModel.cpp)
target_sources(ForgePilotCommunication PRIVATE
    src/workbench/MissionProtocol.h src/workbench/MissionProtocol.cpp
    src/workbench/WorkbenchListModel.h src/workbench/WorkbenchListModel.cpp
    src/workbench/MissionListModel.h src/workbench/MissionListModel.cpp
    src/workbench/GraphModel.h src/workbench/GraphModel.cpp
    src/workbench/ArtifactModel.h src/workbench/ArtifactModel.cpp
    src/workbench/EvidenceModel.h src/workbench/EvidenceModel.cpp
    src/workbench/DiffModel.h src/workbench/DiffModel.cpp
    src/workbench/ValidationModel.h src/workbench/ValidationModel.cpp
    src/workbench/ApprovalController.h src/workbench/ApprovalController.cpp
    src/workbench/DeliveryController.h src/workbench/DeliveryController.cpp
    src/workbench/MissionController.h src/workbench/MissionController.cpp)
target_include_directories(ForgePilotCommunication PUBLIC src)
target_link_libraries(ForgePilotCommunication PUBLIC Qt6::Network Qt6::Concurrent Qt6::Gui)
target_compile_features(ForgePilotCommunication PUBLIC cxx_std_20)
target_compile_options(ForgePilotCommunication PUBLIC /utf-8)
set_target_properties(ForgePilotCommunication PROPERTIES CXX_EXTENSIONS OFF)

qt_add_executable(ForgePilotClient WIN32 main.cpp)
set_target_properties(ForgePilotClient PROPERTIES CXX_EXTENSIONS OFF)
target_link_libraries(ForgePilotClient PRIVATE ForgePilotCommunication Qt6::Quick Qt6::Qml Qt6::QuickControls2 Qt6::QuickDialogs2)
qt_add_qml_module(ForgePilotClient URI ForgePilot VERSION 1.0 QML_FILES Main.qml
    panels/ConversationPanel.qml panels/ConversationHistoryPanel.qml panels/PlanPanel.qml panels/TracePanel.qml panels/BuildPanel.qml
    panels/DiffPanel.qml panels/RepositoryPanel.qml panels/TaskHistoryPanel.qml panels/SettingsPanel.qml
    panels/McpSettingsPanel.qml panels/McpServerDialog.qml panels/McpToolsDialog.qml
    panels/Panel.qml panels/StatusBadge.qml panels/LongText.qml panels/Ui.js panels/RagUi.js
    panels/Theme.js panels/DarkPalette.qml panels/ActionButton.qml panels/AppScrollBar.qml panels/AppScrollView.qml panels/TextMenu.qml panels/NavigationTab.qml panels/MemoryDialog.qml)

# Ship the existing Qt Chinese catalogs inside the executables, independent of OS locale.
get_target_property(qmake_path Qt6::qmake IMPORTED_LOCATION)
execute_process(COMMAND "${qmake_path}" -query QT_INSTALL_TRANSLATIONS
    OUTPUT_VARIABLE qt_translations OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
function(forgepilot_ui target)
    qt_add_resources(${target} qt_chinese PREFIX "/translations" BASE "${qt_translations}"
        FILES "${qt_translations}/qtbase_zh_CN.qm" "${qt_translations}/qtdeclarative_zh_CN.qm")
endfunction()
forgepilot_ui(ForgePilotClient)

include(CTest)
if(BUILD_TESTING)
    find_package(Qt6 REQUIRED COMPONENTS Test)
    qt_add_executable(ForgePilotClientTests tests/tst_communication.cpp)
    target_link_libraries(ForgePilotClientTests PRIVATE ForgePilotCommunication Qt6::Test Qt6::Quick Qt6::Qml)
    add_test(NAME client_communication COMMAND ForgePilotClientTests -o "${CMAKE_CURRENT_BINARY_DIR}/communication-results.txt,txt")
    # The communication executable intentionally contains the complete network,
    # controller, and streaming regression suite.  It exceeds one minute on
    # Windows debug/CI hosts even when every individual case is healthy.
    set_tests_properties(client_communication PROPERTIES TIMEOUT 120)
    set_target_properties(ForgePilotClientTests PROPERTIES WIN32_EXECUTABLE FALSE)
    foreach(suite mission_api mission_sse workbench_models mission_controller)
        qt_add_executable(ForgePilot_${suite}_tests tests/tst_${suite}.cpp)
        target_link_libraries(ForgePilot_${suite}_tests PRIVATE ForgePilotCommunication Qt6::Test)
        target_compile_options(ForgePilot_${suite}_tests PRIVATE /utf-8)
        target_compile_definitions(ForgePilot_${suite}_tests PRIVATE FORGEPILOT_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/..")
        set_target_properties(ForgePilot_${suite}_tests PROPERTIES WIN32_EXECUTABLE FALSE)
        add_test(NAME client_${suite} COMMAND ForgePilot_${suite}_tests -o "${CMAKE_CURRENT_BINARY_DIR}/${suite}-results.txt,txt")
        set_tests_properties(client_${suite} PROPERTIES TIMEOUT 120)
    endforeach()
    function(forgepilot_backend_test target source)
        qt_add_executable(${target} ${source})
        target_link_libraries(${target} PRIVATE ForgePilotCommunication Qt6::Test Qt6::Quick Qt6::Qml Qt6::QuickControls2)
        forgepilot_ui(${target})
        set_target_properties(${target} PROPERTIES WIN32_EXECUTABLE FALSE)
    endfunction()
    forgepilot_backend_test(ForgePilotBackendTests tests/tst_backend.cpp)
    forgepilot_backend_test(ForgePilotConversationTests tests/tst_conversations_backend.cpp)
    forgepilot_backend_test(ForgePilotDeletionTests tests/tst_deletion_backend.cpp)
    forgepilot_backend_test(ForgePilotMemoryTests tests/tst_memory_backend.cpp)
    forgepilot_backend_test(ForgePilotMcpTests tests/tst_mcp_backend.cpp)
    forgepilot_backend_test(ForgePilotMissionBackendTests tests/tst_mission_backend.cpp)
endif()

message(STATUS "ForgePilot Qt: ${Qt6_VERSION}")
message(STATUS "ForgePilot Qt CMake directory: ${Qt6_DIR}")
message(STATUS "ForgePilot compiler: ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}")
