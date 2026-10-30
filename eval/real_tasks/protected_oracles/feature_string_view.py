import json
from pathlib import Path
import shutil
import tempfile

from common import command, guarded, repository_argument, run_cpp_oracle


SOURCE = r'''
#include <pugixml.hpp>
#include <string_view>
#ifndef PUGIXML_HAS_STRING_VIEW
#error C++17 string_view support must be available without an opt-in macro
#endif
int main() {
  pugi::xml_document document;
  if (!document.load_string("<root><item id='7'/></root>")) return 1;
  std::string_view root("root");
  std::string_view item("item");
  auto node = document.child(root).child(item);
  return node && node.attribute("id").as_int() == 7 ? 0 : 1;
}
'''


def _reply_json(reply, name):
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".json"):
        raise ValueError("Invalid CMake file API reply")
    return json.loads((reply / name).read_text(encoding="utf-8"))


def _default_cxx17(reply):
    indexes = sorted(reply.glob("index-*.json"))
    if len(indexes) != 1:
        return False
    index = json.loads(indexes[0].read_text(encoding="utf-8"))
    codemodel = _reply_json(reply, index["reply"]["codemodel-v2"]["jsonFile"])
    for configuration in codemodel["configurations"]:
        targets = [target for target in configuration["targets"]
                   if target["name"] in {"pugixml-static", "pugixml"}]
        for target in targets:
            details = _reply_json(reply, target["jsonFile"])
            for group in details.get("compileGroups", []):
                if group.get("language") == "CXX" and group.get("languageStandard", {}).get("standard") == "17":
                    return True
    return False


def check_cmake_standards(args):
    cmake = shutil.which(args.cmake)
    if not cmake:
        return "tool_missing"
    with tempfile.TemporaryDirectory(prefix="forgepilot-protected-cmake-") as scratch_name:
        scratch = Path(scratch_name).resolve()
        if scratch.is_relative_to(args.repository) or args.repository.is_relative_to(scratch):
            return "isolation_failed"
        build = scratch / "build-default"
        query = build / ".cmake" / "api" / "v1" / "query" / "codemodel-v2"
        query.parent.mkdir(parents=True)
        query.touch()
        configure = [cmake, "-S", str(args.repository), "-B", str(build),
                     "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DPUGIXML_BUILD_TESTS=OFF"]
        if args.generator:
            configure.extend(["-G", args.generator])
        if not command(configure, scratch):
            return "configure_failed"
        if not _default_cxx17(build / ".cmake" / "api" / "v1" / "reply"):
            return "cmake_default_incorrect"
        older_build = scratch / "build-cxx11"
        older = [cmake, "-S", str(args.repository), "-B", str(older_build),
                 "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DPUGIXML_BUILD_TESTS=OFF",
                 "-DCMAKE_CXX_STANDARD=11", "-DCMAKE_CXX_STANDARD_REQUIRED=ON"]
        if args.generator:
            older.extend(["-G", args.generator])
        if not command(older, scratch):
            return "older_mode_configure_failed"
        if not command([cmake, "--build", str(older_build), "--config", "Debug"], scratch):
            return "older_mode_build_failed"
    return "passed"


def check(args):
    status = run_cpp_oracle(args, SOURCE, include_dir="src",
                            extra_sources=("src/pugixml.cpp",), standard=17)
    return check_cmake_standards(args) if status == "passed" else status


if __name__ == "__main__":
    raise SystemExit(guarded(lambda: check(repository_argument())))
