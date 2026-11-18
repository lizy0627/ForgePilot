// Evaluator-owned launcher. The Python grader creates the profile and grants
// narrowly scoped ACLs before this process is started by the build Job Object.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <userenv.h>
#include <string>
#include <vector>
#include <algorithm>
#include <cstdio>

static std::wstring quote(const wchar_t* input) {
    std::wstring out = L"\"";
    unsigned backslashes = 0;
    for (const wchar_t* p = input; *p; ++p) {
        if (*p == L'\\') { ++backslashes; continue; }
        if (*p == L'"') {
            out.append(backslashes * 2 + 1, L'\\');
            out.push_back(*p);
        } else {
            out.append(backslashes, L'\\');
            out.push_back(*p);
        }
        backslashes = 0;
    }
    out.append(backslashes * 2, L'\\');
    out.push_back(L'"');
    return out;
}

int wmain(int argc, wchar_t** argv) {
    if (argc < 3) return 125;
    PSID sid = nullptr;
    HRESULT hr = DeriveAppContainerSidFromAppContainerName(argv[1], &sid);
    if (FAILED(hr) || !sid) {
        fwprintf(stderr, L"appcontainer-launcher: profile SID unavailable (HRESULT 0x%08lx)\n", (unsigned long)hr);
        return 125;
    }
    SIZE_T bytes = 0;
    InitializeProcThreadAttributeList(nullptr, 2, 0, &bytes);
    auto attributes = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(HeapAlloc(GetProcessHeap(), 0, bytes));
    if (!attributes || !InitializeProcThreadAttributeList(attributes, 2, 0, &bytes)) {
        fwprintf(stderr, L"appcontainer-launcher: attribute setup failed (%lu)\n", GetLastError());
        FreeSid(sid);
        return 125;
    }
    SECURITY_CAPABILITIES caps = {};
    caps.AppContainerSid = sid;
    if (!UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                                   &caps, sizeof(caps), nullptr, nullptr)) {
        fwprintf(stderr, L"appcontainer-launcher: capability setup failed (%lu)\n", GetLastError());
        DeleteProcThreadAttributeList(attributes);
        HeapFree(GetProcessHeap(), 0, attributes);
        FreeSid(sid);
        return 125;
    }
    std::wstring command;
    for (int i = 2; i < argc; ++i) {
        if (i != 2) command.push_back(L' ');
        command += quote(argv[i]);
    }
    std::vector<wchar_t> writable(command.begin(), command.end());
    writable.push_back(0);
    STARTUPINFOEXW startup = {};
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.StartupInfo.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.StartupInfo.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    startup.lpAttributeList = attributes;
    std::vector<HANDLE> inherited;
    for (HANDLE handle : {startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput,
                          startup.StartupInfo.hStdError}) {
        if (!handle || handle == INVALID_HANDLE_VALUE ||
            !SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)) {
            fwprintf(stderr, L"appcontainer-launcher: standard handle setup failed (%lu)\n", GetLastError());
            DeleteProcThreadAttributeList(attributes);
            HeapFree(GetProcessHeap(), 0, attributes);
            FreeSid(sid);
            return 125;
        }
        if (std::find(inherited.begin(), inherited.end(), handle) == inherited.end())
            inherited.push_back(handle);
    }
    if (!UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                   inherited.data(), inherited.size() * sizeof(HANDLE), nullptr, nullptr)) {
        fwprintf(stderr, L"appcontainer-launcher: handle allowlist failed (%lu)\n", GetLastError());
        DeleteProcThreadAttributeList(attributes);
        HeapFree(GetProcessHeap(), 0, attributes);
        FreeSid(sid);
        return 125;
    }
    PROCESS_INFORMATION process = {};
    BOOL launched = CreateProcessW(argv[2], writable.data(), nullptr, nullptr, TRUE,
                                   EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW,
                                   nullptr, nullptr, &startup.StartupInfo, &process);
    DWORD status = 125;
    if (!launched) {
        fwprintf(stderr, L"appcontainer-launcher: child launch failed (%lu)\n", GetLastError());
    } else {
        WaitForSingleObject(process.hProcess, INFINITE);
        GetExitCodeProcess(process.hProcess, &status);
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
    }
    DeleteProcThreadAttributeList(attributes);
    HeapFree(GetProcessHeap(), 0, attributes);
    FreeSid(sid);
    return static_cast<int>(status);
}
