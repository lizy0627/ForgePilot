# Button status Mission demo

Create a standalone Git repository from `qt-button-status`:

```powershell
.\scripts\Initialize-ButtonStatusDemo.ps1 -Destination D:\Development\ForgePilotButtonStatusDemo -QtKitRoot D:\Qt\6.8.3\msvc2022_64
```

The baseline has a deliberate bug. Configure and build with Qt 6, then run CTest; `button_updates_status` must fail because the label remains `Ready`. Keep that baseline commit for repeated Missions. The protected CMake project in `qt-button-status-oracle` is bound to this demo's `CMakeLists.txt` hash and must stay outside the standalone repository.

The initializer sets repository-local `core.autocrlf=false` so task Worktrees keep
the exact CMake bytes required by the Oracle. For a demo created with an older
initializer, run `git -C <demo-path> config core.autocrlf false` before creating a
new Mission. The configuration preflight rejects conversion-enabled repositories;
changing this setting does not repair an already-created Worktree.

For API v2, set the trusted launcher's `--mission-oracle` to the absolute `qt-button-status-oracle` directory. Put a trusted `QtKit.cmake` file in that Oracle directory with `list(PREPEND CMAKE_PREFIX_PATH "<Qt kit root>")`. The initializer commits the matching file into the standalone repository, so the isolated validator can find Qt even though it clears arbitrary environment variables. Use explicit `--generator` and `--architecture`; Mission validation does not accept preset flags. On Windows with the Visual Studio generator, set `FORGEPILOT_ARTIFACT_ROOT` to a short path such as `D:\FPDemo\artifacts` before launch so MSBuild's generated file paths stay within its 260-character limit. Check the local setup without printing credentials:

```powershell
$env:FORGEPILOT_ARTIFACT_ROOT = 'D:\FPDemo\artifacts'
.\scripts\Check-ButtonStatusMission.ps1 -Repository D:\Development\ForgePilotButtonStatusDemo -LauncherConfig "$env:LOCALAPPDATA\ForgePilot\launcher\<identity>\config.json"
.\scripts\Start-ButtonStatusMission.ps1 -ArtifactRoot D:\FPDemo\artifacts
```

Create a v2 `bug_fix` Mission against the standalone repository and `HEAD`, using the request in `PROJECT.txt`. Stop at the execution approval page to inspect the TaskSpec, plan and scope. To repeat the demo, create a fresh standalone repository at a new path or restore the baseline commit after completing or cancelling the previous Mission.

When the configured cloud model is unavailable, `offline_mission_provider.py` can provide deterministic local responses for testing the Mission API and desktop approval page. This is an offline transport and workflow check; it does not establish real model quality or authorize execution. A Mission freezes its model configuration hash, so keep the offline route for reopening this offline Mission; create a new Mission after restoring the normal model route for a real run.
