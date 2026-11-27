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
    src/BackendConnection.h src/BackendConnection.cpp
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
    src/RuntimeConfigController.h src/RuntimeConfigController.cpp
    src/RecordListModel.h src/RecordListModel.cpp
    src/RepositoryModel.h src/RepositoryModel.cpp)
target_sources(ForgePilotCommunication PRIVATE
    src/workbench/MissionProtocol.h src/workbench/MissionProtocol.cpp
    src/workbench/CreateMissionRequest.h src/workbench/CreateMissionRequest.cpp
    src/workbench/SkillDraftClient.h src/workbench/SkillDraftClient.cpp
    src/workbench/WorkbenchListModel.h src/workbench/WorkbenchListModel.cpp
    src/workbench/MissionListModel.h src/workbench/MissionListModel.cpp
    src/workbench/GraphModel.h src/workbench/GraphModel.cpp
    src/workbench/ArtifactModel.h src/workbench/ArtifactModel.cpp
    src/workbench/EvidenceModel.h src/workbench/EvidenceModel.cpp
    src/workbench/DiffModel.h src/workbench/DiffModel.cpp
    src/workbench/ValidationModel.h src/workbench/ValidationModel.cpp
    src/workbench/ApprovalController.h src/workbench/ApprovalController.cpp
    src/workbench/DeliveryController.h src/workbench/DeliveryController.cpp
    src/workbench/MissionSnapshotLoader.h src/workbench/MissionSnapshotLoader.cpp
    src/workbench/MissionController.h src/workbench/MissionController.cpp)
# Embed the exported request schema so installed clients need no protocol files.
set(openapi_path "${CMAKE_CURRENT_SOURCE_DIR}/../protocol/openapi.json")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${openapi_path}")
file(READ "${openapi_path}" openapi)
string(JSON mission_ref GET "${openapi}" paths /api/v2/missions post requestBody content application/json schema "$ref")
get_filename_component(mission_model "${mission_ref}" NAME)
string(JSON create_mission_schema GET "${openapi}" components schemas "${mission_model}")
string(JSON max_selected_skills GET "${create_mission_schema}" properties selected_skills maxItems)
string(JSON skill_ref GET "${create_mission_schema}" properties selected_skills items "$ref")
get_filename_component(skill_model "${skill_ref}" NAME)
string(JSON selected_skill_schema GET "${openapi}" components schemas "${skill_model}")
string(JSON create_mission_schema SET "${create_mission_schema}" properties selected_skills items "${selected_skill_schema}")
configure_file(src/workbench/CreateMissionContract.h.in generated/CreateMissionContract.h @ONLY)
target_include_directories(ForgePilotCommunication PUBLIC src "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_link_libraries(ForgePilotCommunication PUBLIC Qt6::Network Qt6::Concurrent Qt6::Gui)
target_compile_features(ForgePilotCommunication PUBLIC cxx_std_20)
target_compile_options(ForgePilotCommunication PUBLIC /utf-8)
set_target_properties(ForgePilotCommunication PROPERTIES CXX_EXTENSIONS OFF)

qt_add_executable(ForgePilotClient WIN32 main.cpp)
set_target_properties(ForgePilotClient PROPERTIES CXX_EXTENSIONS OFF)
target_link_libraries(ForgePilotClient PRIVATE ForgePilotCommunication Qt6::Quick Qt6::Qml Qt6::QuickControls2 Qt6::QuickDialogs2)
qt_add_qml_module(ForgePilotClient URI ForgePilot VERSION 1.0 QML_FILES Main.qml
    panels/ConversationPanel.qml panels/ConversationHistoryPanel.qml panels/PlanPanel.qml panels/TracePanel.qml panels/BuildPanel.qml
    panels/DiffPanel.qml panels/RepositoryPanel.qml panels/TaskHistoryPanel.qml panels/SettingsPanel.qml panels/RuntimeConfigDialog.qml
    panels/McpSettingsPanel.qml panels/McpServerDialog.qml panels/McpToolsDialog.qml
    panels/Panel.qml panels/StatusBadge.qml panels/LongText.qml panels/Ui.js panels/RagUi.js
    panels/AppCheckBox.qml panels/AppDialog.qml panels/AppDialogButtonBox.qml panels/AppItemDelegate.qml panels/AppSwitch.qml panels/AppMenuItem.qml
    panels/Theme.js panels/DarkPalette.qml panels/ActionButton.qml panels/AppTextField.qml panels/AppTextArea.qml panels/AppComboBox.qml panels/AppScrollBar.qml panels/AppScrollView.qml panels/TextMenu.qml panels/NavigationTab.qml panels/MemoryDialog.qml
    workbench/WorkbenchPage.qml workbench/TaskCenter.qml workbench/MissionHistory.qml workbench/MissionHeader.qml workbench/TaskGraphView.qml
    workbench/WorkflowPage.qml workbench/WorkflowGraph.qml workbench/WorkflowDetails.qml workbench/WorkflowUi.js workbench/WorkflowTheme.js
    workbench/SkillDraftPanel.qml workbench/SkillDraftEditor.qml
    workbench/ActivityTimeline.qml workbench/TaskSpecPanel.qml workbench/EvidencePanel.qml
    workbench/ExecutionPlanPanel.qml workbench/ApprovalPanel.qml workbench/MissionDiffPanel.qml
    workbench/ValidationPanel.qml workbench/ReviewPanel.qml workbench/DeliveryPanel.qml
    workbench/StateNotice.qml workbench/NodeStatusBadge.qml workbench/WorkbenchUi.js
    RESOURCES assets/forge-mark.svg assets/fluent/LICENSE
    assets/fluent/add.svg assets/fluent/arrow-right.svg assets/fluent/brain.svg assets/fluent/chat.svg
    assets/fluent/check.svg assets/fluent/chevron-down.svg assets/fluent/chevron-right.svg assets/fluent/code.svg
    assets/fluent/copy.svg assets/fluent/delete.svg assets/fluent/dismiss.svg assets/fluent/document.svg
    assets/fluent/edit.svg assets/fluent/flow.svg assets/fluent/folder.svg assets/fluent/history.svg
    assets/fluent/library.svg assets/fluent/more.svg assets/fluent/refresh.svg assets/fluent/send.svg
    assets/fluent/settings.svg assets/fluent/sidebar.svg assets/fluent/stop.svg)

# Ship the existing Qt Chinese catalogs inside the executables, independent of OS locale.
get_target_property(qmake_path Qt6::qmake IMPORTED_LOCATION)
execute_process(COMMAND "${qmake_path}" -query QT_INSTALL_TRANSLATIONS
    OUTPUT_VARIABLE qt_translations OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
function(forgepilot_ui target)
    qt_add_resources(${target} ${target}_qt_chinese PREFIX "/translations" BASE "${qt_translations}"
        FILES "${qt_translations}/qtbase_zh_CN.qm" "${qt_translations}/qtdeclarative_zh_CN.qm")
endfunction()
forgepilot_ui(ForgePilotClient)

include(CTest)
if(BUILD_TESTING)
    find_package(Qt6 REQUIRED COMPONENTS Test)
    qt_add_executable(ForgePilotClientTests tests/tst_communication.cpp)
    target_link_libraries(ForgePilotClientTests PRIVATE ForgePilotCommunication Qt6::Test Qt6::Quick Qt6::Qml)
    target_compile_definitions(ForgePilotClientTests PRIVATE FORGEPILOT_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/..")
    add_test(NAME client_communication COMMAND ForgePilotClientTests -o "${CMAKE_CURRENT_BINARY_DIR}/communication-results.txt,txt")
    # The communication executable intentionally contains the complete network,
    # controller, and streaming regression suite.  It exceeds one minute on
    # Windows debug/CI hosts even when every individual case is healthy.
    set_tests_properties(client_communication PROPERTIES TIMEOUT 120)
    set_target_properties(ForgePilotClientTests PROPERTIES WIN32_EXECUTABLE FALSE)
    qt_add_executable(ForgePilotRuntimeConfigTests tests/tst_runtime_config.cpp)
    target_link_libraries(ForgePilotRuntimeConfigTests PRIVATE ForgePilotCommunication Qt6::Test Qt6::Quick Qt6::Qml Qt6::QuickControls2)
    target_compile_options(ForgePilotRuntimeConfigTests PRIVATE /utf-8)
    target_compile_definitions(ForgePilotRuntimeConfigTests PRIVATE FORGEPILOT_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/..")
    set_target_properties(ForgePilotRuntimeConfigTests PROPERTIES WIN32_EXECUTABLE FALSE)
    add_test(NAME client_runtime_config COMMAND ForgePilotRuntimeConfigTests -o "${CMAKE_CURRENT_BINARY_DIR}/runtime_config-results.txt,txt")
    set_tests_properties(client_runtime_config PROPERTIES TIMEOUT 120 ENVIRONMENT "QT_QPA_PLATFORM=offscreen")
    foreach(suite mission_api mission_sse workbench_models mission_controller mission_snapshot)
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
    add_test(NAME client_settings_qml COMMAND ForgePilotBackendTests settingsSwitchesBackendIndependently
        codeIndexMinimalPanel codeIndexPanelDoesNotShowStaleReadyStatus mcpSettingsPanelDistinguishesUnreadFailureAndDisabled)
    set_tests_properties(client_settings_qml PROPERTIES TIMEOUT 120 ENVIRONMENT "QT_QPA_PLATFORM=offscreen")
    forgepilot_backend_test(ForgePilotConversationTests tests/tst_conversations_backend.cpp)
    forgepilot_backend_test(ForgePilotDeletionTests tests/tst_deletion_backend.cpp)
    forgepilot_backend_test(ForgePilotMemoryTests tests/tst_memory_backend.cpp)
    forgepilot_backend_test(ForgePilotMcpTests tests/tst_mcp_backend.cpp)
    forgepilot_backend_test(ForgePilotMissionBackendTests tests/tst_mission_backend.cpp)
    forgepilot_backend_test(ForgePilotWorkbenchQmlTests tests/tst_workbench_qml.cpp)
    forgepilot_backend_test(ForgePilotWorkflowQmlTests tests/tst_workflow_qml.cpp)
    add_dependencies(ForgePilotWorkflowQmlTests ForgePilotClient)
    target_compile_options(ForgePilotWorkflowQmlTests PRIVATE /utf-8)
    target_compile_definitions(ForgePilotWorkflowQmlTests PRIVATE FORGEPILOT_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/..")
    add_test(NAME client_workflow_qml COMMAND ForgePilotWorkflowQmlTests -o "${CMAKE_CURRENT_BINARY_DIR}/workflow_qml-results.txt,txt")
    set_tests_properties(client_workflow_qml PROPERTIES TIMEOUT 120 ENVIRONMENT "QT_QPA_PLATFORM=offscreen")
    target_compile_definitions(ForgePilotWorkbenchQmlTests PRIVATE FORGEPILOT_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/..")
    set(qt_test_deploy_args --test --include-plugins qoffscreen)
    add_test(NAME client_workbench_qml COMMAND ForgePilotWorkbenchQmlTests -o "${CMAKE_CURRENT_BINARY_DIR}/workbench_qml-results.txt,txt")
    set_tests_properties(client_workbench_qml PROPERTIES TIMEOUT 120 ENVIRONMENT "QT_QPA_PLATFORM=offscreen")
endif()

# Every executable uses the same output directory and communication library.
# Give the shared runtime one writer, including when building a single test.
get_filename_component(qt_bin_dir "${qmake_path}" DIRECTORY)
add_custom_target(ForgePilotQtRuntime
    COMMAND "${qt_bin_dir}/windeployqt.exe" --$<IF:$<CONFIG:Debug>,debug,release>
        --dir "$<TARGET_FILE_DIR:ForgePilotClient>" --qmldir "${CMAKE_CURRENT_SOURCE_DIR}"
        --no-translations --concurrent --quickcontrols2 --quickdialogs2 ${qt_test_deploy_args}
        "$<TARGET_FILE:Qt6::Quick>"
    COMMENT "Deploying shared Qt runtime"
    VERBATIM)
add_dependencies(ForgePilotCommunication ForgePilotQtRuntime)

message(STATUS "ForgePilot Qt: ${Qt6_VERSION}")
message(STATUS "ForgePilot Qt CMake directory: ${Qt6_DIR}")
message(STATUS "ForgePilot compiler: ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}")
