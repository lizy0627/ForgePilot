"""Supplementary process-external behavioral probes for locked C++ tasks.

The evaluator owns expected results and sends fresh cases over stdin only after
the probe process starts.  Candidate code runs in the probe's restricted domain;
Oracle source and expected results are never compiled into that process.  A
candidate static initializer that exits before ``main`` yields no valid frame.

This is a narrow prototype.  Unsupported task IDs fail closed, and the caller
must still run the original locked Oracle and all public/integrity checks.
"""

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import random
import re
import secrets


_FRAME = "FP-PROBE-1"
_NONCE = re.compile(r"[0-9a-f]{32}")


def _discount(cents: int, percent: int) -> int:
    return cents * (100 - percent) // 100


_INTEGER_CONTRACTS = {
    # arity, C++ invocation, reference calculation, locked Oracle cases
    "cpp-compile-01": (2, "add(a, b)", lambda a, b: a + b,
                       ((2, 3), (-7, 4), (0, 0))),
    "cpp-compile-02": (1, "square(a)", lambda a: a * a, ((4,), (-3,), (0,))),
    "cpp-compile-03": (1, "math::twice(a)", lambda a: 2 * a, ((5,), (-4,))),
    "cpp-logic-01": (1, "clamp_level(a)", lambda a: min(10, max(0, a)),
                     tuple((n,) for n in range(-12, 23))),
    "cpp-logic-02": (3, "max_of_three(a, b, c)", max,
                     ((-9, -2, -5), (4, 2, 3), (-1, -1, -1))),
    "cpp-logic-03": (2, "discounted(a, b)", _discount,
                     ((199, 10), (250, 20), (99, 0), (50, 100))),
    "cmake-01": (1, "calculate(a)", lambda a: a * 3 + 1, ((3,), (-2,))),
    "cmake-02": (1, "calculate(a)", lambda a: a + 7, ((1,), (-7,))),
    "repair-01": (1, "positive_part(a)", lambda a: max(0, a),
                  ((-3,), (0,), (8,))),
    "unit-test-02": (1, "leap_year(a)",
                     lambda a: int(a % 400 == 0 or (a % 4 == 0 and a % 100 != 0)),
                     ((1900,), (2000,), (2024,), (2023,), (2100,))),
}


@dataclass(frozen=True)
class ProbeCase:
    request: str
    expected: str


@dataclass(frozen=True)
class ProbeChallenge:
    task_id: str
    nonce: str
    cases: tuple[ProbeCase, ...]

    @property
    def stdin(self) -> bytes:
        lines = [f"{self.nonce} {len(self.cases)}", *(case.request for case in self.cases)]
        return ("\n".join(lines) + "\n").encode("ascii")


def _hex(value: str) -> str:
    return value.encode("utf-8").hex()


def make_challenge(task_id: str, *, seed: int, nonce: str | None = None) -> ProbeChallenge:
    """Build reproducible hidden cases; the nonce binds one process response."""
    if type(seed) is not int or seed < 0:
        raise ValueError("Probe seed must be a non-negative integer")
    nonce = secrets.token_hex(16) if nonce is None else nonce
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ValueError("Invalid probe nonce")
    rng = random.Random(seed)
    if task_id in _INTEGER_CONTRACTS:
        arity, _, reference, locked = _INTEGER_CONTRACTS[task_id]
        if task_id == "cpp-logic-03":
            extra = [(rng.randint(0, 10_000), rng.randint(0, 100)) for _ in range(9)]
        elif task_id == "unit-test-02":
            extra = [(rng.randint(1, 3_000),) for _ in range(9)]
        else:
            extra = [tuple(rng.randint(-100, 100) for _ in range(arity)) for _ in range(9)]
        cases = tuple(ProbeCase(" ".join(map(str, args)), str(reference(*args)))
                      for args in (*locked, *extra))
    elif task_id == "cross-file-01":
        values = [(0, 3, 4), (0, 7, 0), (1, 9, 2)]
        values.extend((rng.randrange(2), rng.randint(-100, 100), rng.randint(0, 100))
                      for _ in range(9))
        cases = tuple(ProbeCase(f"{op} {a} {b}", str(a * b)) for op, a, b in values)
    elif task_id == "unit-test-01":
        values = ["12x", "-1", " 3", "", "012", "2147483648", "0"]
        values.extend(str(rng.randint(0, 2_147_483_647)) for _ in range(9))
        cases = tuple(ProbeCase(f"{len(value)} {value.encode('ascii').hex() or '-'}",
                                str(int(value) if value.isascii() and value.isdecimal()
                                    and int(value) <= 2_147_483_647 else -1))
                      for value in values)
    elif task_id in {"qt-signal-01", "qt-signal-02"}:
        values = [1, 2, 3, 4, 5, *(rng.randint(1, 12) for _ in range(9))]
        cases = tuple(ProbeCase(str(n), f"{n} {n} " + " ".join(map(str, range(1, n + 1))))
                      for n in values)
    elif task_id == "real-bug-01":
        tokens = ("-z", "--mystery", "-q")
        sequences = [tokens, ("-z",), ("--mystery", "-q")]
        sequences.extend(tuple(rng.sample(tokens, rng.randint(1, len(tokens))))
                         for _ in range(9))
        cases = tuple(ProbeCase(f"{len(items)} {' '.join(items)}",
                                f"{len(items)} {' '.join(_hex(item) for item in items)}")
                      for items in sequences)
    elif task_id == "real-bug-03":
        values = [("2.5", True), ("nan", True), ("2.5typo", False), ("nanXYZ", False)]
        values.extend((f"{rng.randint(1, 999)}.{rng.randint(0, 999)}", True)
                      for _ in range(5))
        values.extend((f"{rng.randint(1, 999)}.{rng.randint(0, 999)}xyz", False)
                      for _ in range(5))
        cases = tuple(ProbeCase(value, str(int(expected))) for value, expected in values)
    elif task_id == "real-feature-01":
        sequences = [(("value", "one"), ("flag", "1"), ("value", "two")),
                     (("flag", "1"), ("value", "three")),
                     (("value", "four"), ("value", "five"))]
        words = ("red", "blue", "green", "amber", "violet")
        for _ in range(9):
            count = rng.randint(1, 5)
            sequences.append(tuple(("flag", "1") if rng.randrange(3) == 0 else
                                   ("value", rng.choice(words)) for _ in range(count)))
        cases = []
        for sequence in sequences:
            argv = []
            for key, value in sequence:
                argv.extend(("--flag",) if key == "flag" else ("--value", value))
            expected = " ".join([str(len(sequence)),
                *(part for key, value in sequence for part in (_hex(key), _hex(value)))])
            cases.append(ProbeCase(f"{len(argv)} {' '.join(argv)}", expected))
        cases = tuple(cases)
    elif task_id == "real-feature-02":
        cases = tuple(ProbeCase(f"{rng.randint(1, 99_999)}", "1") for _ in range(12))
    else:
        raise ValueError("No process-external probe contract for locked task")
    return ProbeChallenge(task_id, nonce, cases)


def verify_response(challenge: ProbeChallenge, *, returncode: int, stdout: bytes) -> bool:
    """Require every framed response; exit(0) without probe main is a failure."""
    if type(returncode) is not int or returncode != 0 or not isinstance(stdout, bytes) or len(stdout) > 65_536:
        return False
    try:
        lines = stdout.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError:
        return False
    if len(lines) != len(challenge.cases) + 1 or lines[0] != f"{_FRAME} {challenge.nonce} {len(challenge.cases)}":
        return False
    return all(line == f"{index} {case.expected}"
               for index, (line, case) in enumerate(zip(lines[1:], challenge.cases, strict=True)))


_ADD_SOURCE = r'''
#include "subject.h"
#include <iostream>
#include <string>
int main() {
  std::string nonce;
  int count = 0;
  if (!(std::cin >> nonce >> count) || count < 1 || count > 64) return 70;
  std::cout << "FP-PROBE-1 " << nonce << " " << count << "\n";
  for (int i = 0; i < count; ++i) {
    int a = 0, b = 0;
    if (!(std::cin >> a >> b)) return 71;
    std::cout << i << " " << add(a, b) << "\n";
  }
  return 0;
}
'''


def _integer_source(task_id: str) -> str:
    arity, expression, _, _ = _INTEGER_CONTRACTS[task_id]
    variables = ("a", "b", "c")[:arity]
    declarations = " ".join(f"int {name} = 0;" for name in variables)
    reads = " >> ".join(variables)
    return f'''#include "subject.h"
#include <iostream>
#include <string>
int main() {{
  std::string nonce; int count = 0;
  if (!(std::cin >> nonce >> count) || count < 1 || count > 64) return 70;
  std::cout << "FP-PROBE-1 " << nonce << " " << count << "\\n";
  for (int i = 0; i < count; ++i) {{
    {declarations}
    if (!(std::cin >> {reads})) return 71;
    std::cout << i << " " << {expression} << "\\n";
  }}
  return 0;
}}
'''


_CROSS_FILE_SOURCE = r'''
#include "subject.h"
#include <iostream>
#include <string>
int main() {
  std::string nonce; int count = 0;
  if (!(std::cin >> nonce >> count) || count < 1 || count > 64) return 70;
  std::cout << "FP-PROBE-1 " << nonce << " " << count << "\n";
  for (int i = 0; i < count; ++i) {
    int op = 0, a = 0, b = 0;
    if (!(std::cin >> op >> a >> b) || (op != 0 && op != 1)) return 71;
    std::cout << i << " " << (op == 0 ? order_total(a, b) : subtotal(a, b)) << "\n";
  }
  return 0;
}
'''


_PARSE_SOURCE = r'''
#include "subject.h"
#include <iostream>
#include <string>
int main() {
  std::string nonce; int count = 0;
  if (!(std::cin >> nonce >> count) || count < 1 || count > 64) return 70;
  std::cout << "FP-PROBE-1 " << nonce << " " << count << "\n";
  for (int i = 0; i < count; ++i) {
    int length = 0; std::string hex;
    if (!(std::cin >> length >> hex) || length < 0 || length > 128) return 71;
    if (length == 0) hex.clear();
    if (hex.size() != static_cast<size_t>(2 * length)) return 72;
    std::string value;
    for (int j = 0; j < length; ++j) {
      unsigned int byte = 0;
      try { byte = std::stoul(hex.substr(2*j, 2), nullptr, 16); }
      catch (...) { return 73; }
      value.push_back(static_cast<char>(byte));
    }
    std::cout << i << " " << parse_count(value) << "\n";
  }
  return 0;
}
'''


_QT_SOURCE = r'''
#include "subject.h"
#include <QCoreApplication>
#include <QSignalSpy>
#include <iostream>
#include <string>
int main(int argc, char** argv) {
  QCoreApplication app(argc, argv);
  std::string nonce; int count = 0;
  if (!(std::cin >> nonce >> count) || count < 1 || count > 64) return 70;
  std::cout << "FP-PROBE-1 " << nonce << " " << count << "\n";
  for (int i = 0; i < count; ++i) {
    int clicks = 0;
    if (!(std::cin >> clicks) || clicks < 1 || clicks > 20) return 71;
    Counter counter; QSignalSpy spy(&counter, &Counter::changed);
    if (!spy.isValid()) return 72;
    for (int j = 0; j < clicks; ++j) counter.click();
    std::cout << i << " " << counter.value() << " " << spy.count();
    for (const auto& event : spy) std::cout << " " << event.at(0).toInt();
    std::cout << "\n";
  }
  return 0;
}
'''


_CXXOPTS_SOURCE = r'''
#include <cxxopts.hpp>
#include <iostream>
#include <string>
#include <vector>
static std::string hex(const std::string& value) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string out;
  for (unsigned char c : value) {
    out += digits[c >> 4];
    out += digits[c & 15];
  }
  return out;
}
int main() {
  std::string nonce;
  int count = 0;
  if (!(std::cin >> nonce >> count) || count < 1 || count > 64) return 70;
  std::cout << "FP-PROBE-1 " << nonce << " " << count << "\n";
  for (int i = 0; i < count; ++i) {
    int length = 0;
    if (!(std::cin >> length) || length < 1 || length > 16) return 71;
    std::vector<std::string> values(static_cast<size_t>(length) + 1);
    values[0] = "probe";
    for (int j = 1; j <= length; ++j) if (!(std::cin >> values[j])) return 72;
    std::vector<char*> argv;
    for (auto& value : values) argv.push_back(&value[0]);
    int argc = static_cast<int>(argv.size());
    char** actual_argv = argv.data();
    cxxopts::Options options("probe", "probe");
    options.allow_unrecognised_options();
    auto result = options.parse(argc, actual_argv);
    const auto unmatched = result.unmatched();
    std::cout << i << " " << unmatched.size();
    for (const auto& item : unmatched) std::cout << " " << hex(item);
    std::cout << "\n";
  }
  return 0;
}
'''


_FLOAT_SOURCE = r'''
#include <cxxopts.hpp>
#include <iostream>
#include <string>
int main() {
  std::string nonce; int count = 0;
  if (!(std::cin >> nonce >> count) || count < 1 || count > 64) return 70;
  std::cout << "FP-PROBE-1 " << nonce << " " << count << "\n";
  for (int i = 0; i < count; ++i) {
    std::string value;
    if (!(std::cin >> value)) return 71;
    cxxopts::Options options("probe", "probe");
    options.add_options()("float", "float", cxxopts::value<float>());
    char name[] = "probe", key[] = "--float";
    char* argv[] = {name, key, &value[0]};
    int argc = 3; bool accepted = false;
    try { options.parse(argc, argv); accepted = true; }
    catch (const cxxopts::exceptions::incorrect_argument_type&) {}
    std::cout << i << " " << (accepted ? 1 : 0) << "\n";
  }
  return 0;
}
'''


_ORDER_SOURCE = r'''
#include <cxxopts.hpp>
#include <iostream>
#include <string>
#include <vector>
static std::string hex(const std::string& value) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string result;
  for (unsigned char c : value) { result += digits[c >> 4]; result += digits[c & 15]; }
  return result;
}
int main() {
  std::string nonce; int count = 0;
  if (!(std::cin >> nonce >> count) || count < 1 || count > 64) return 70;
  std::cout << "FP-PROBE-1 " << nonce << " " << count << "\n";
  for (int i = 0; i < count; ++i) {
    int length = 0;
    if (!(std::cin >> length) || length < 1 || length > 16) return 71;
    std::vector<std::string> values(static_cast<size_t>(length) + 1);
    values[0] = "probe";
    for (int j = 1; j <= length; ++j) if (!(std::cin >> values[j])) return 72;
    std::vector<char*> argv;
    for (auto& value : values) argv.push_back(&value[0]);
    int argc = static_cast<int>(argv.size());
    char** actual_argv = argv.data();
    cxxopts::Options options("probe", "probe");
    options.add_options()("v,value", "value", cxxopts::value<std::string>())
                         ("f,flag", "flag");
    auto result = options.parse(argc, actual_argv);
    const auto& items = result.arguments();
    std::cout << i << " " << items.size();
    for (const auto& item : items) {
      const std::string value = item.key() == "flag" ?
          (item.as<bool>() ? "1" : "0") : item.value();
      std::cout << " " << hex(item.key()) << " " << hex(value);
    }
    std::cout << "\n";
  }
  return 0;
}
'''


_STRING_VIEW_SOURCE = r'''
#include <pugixml.hpp>
#include <iostream>
#include <string>
#include <string_view>
#ifndef PUGIXML_HAS_STRING_VIEW
#error C++17 string_view API unavailable
#endif
int main() {
  std::string nonce; int count = 0;
  if (!(std::cin >> nonce >> count) || count < 1 || count > 64) return 70;
  std::cout << "FP-PROBE-1 " << nonce << " " << count << "\n";
  for (int i = 0; i < count; ++i) {
    int id = 0; if (!(std::cin >> id)) return 71;
    const std::string xml = "<root><item id='" + std::to_string(id) + "'/></root>";
    pugi::xml_document document;
    if (!document.load_string(xml.c_str())) return 72;
    auto node = document.child(std::string_view("root")).child(std::string_view("item"));
    std::cout << i << " " << (node && node.attribute("id").as_int() == id ? 1 : 0) << "\n";
  }
  return 0;
}
'''


def write_probe_project(task_id: str, directory: Path, *, candidate_include: Path,
                        candidate_library: Path | None = None,
                        candidate_source: Path | None = None) -> Path:
    """Create evaluator-owned CMake source; B compiles, C executes elsewhere."""
    micro = task_id in _INTEGER_CONTRACTS or task_id in {
        "cross-file-01", "unit-test-01", "qt-signal-01", "qt-signal-02"}
    if not micro and task_id not in {"real-bug-01", "real-bug-03", "real-feature-01", "real-feature-02"}:
        raise ValueError("No process-external probe contract for locked task")
    if (not directory.is_absolute() or not candidate_include.is_absolute()
            or micro and (candidate_library is None or not candidate_library.is_absolute())
            or task_id.startswith("real-") and candidate_library is not None
            or (task_id == "real-feature-02") != (candidate_source is not None)
            or candidate_source is not None and not candidate_source.is_absolute()):
        raise ValueError("Probe project requires absolute trusted paths")
    for path in (directory, candidate_include, candidate_library, candidate_source):
        if path is not None and any(char in str(path) for char in ('"', ';', '$', '\n', '\r')):
            raise ValueError("Unsafe CMake path")
    directory.mkdir(parents=True, exist_ok=False)
    source = (_integer_source(task_id) if task_id in _INTEGER_CONTRACTS else
              _CROSS_FILE_SOURCE if task_id == "cross-file-01" else
              _PARSE_SOURCE if task_id == "unit-test-01" else
              _QT_SOURCE if task_id.startswith("qt-signal-") else
              _FLOAT_SOURCE if task_id == "real-bug-03" else
              _ORDER_SOURCE if task_id == "real-feature-01" else
              _STRING_VIEW_SOURCE if task_id == "real-feature-02" else _CXXOPTS_SOURCE)
    (directory / "probe.cpp").write_text(source, encoding="utf-8")
    cmake = ["cmake_minimum_required(VERSION 3.24)",
             "project(ForgePilotExternalProbe LANGUAGES CXX)",
             "set(CMAKE_CXX_STANDARD " + ("17" if task_id == "real-feature-02" else
                "11" if task_id.startswith("real-") else "20") + ")",
             "set(CMAKE_CXX_STANDARD_REQUIRED ON)",
             "add_executable(probe probe.cpp)",
             f'target_include_directories(probe PRIVATE "{candidate_include.as_posix()}")']
    if candidate_library is not None:
        cmake.append(f'target_link_libraries(probe PRIVATE "{candidate_library.as_posix()}")')
    if candidate_source is not None:
        cmake.append(f'target_sources(probe PRIVATE "{candidate_source.as_posix()}")')
    if task_id.startswith("qt-signal-"):
        cmake.extend(["find_package(Qt6 REQUIRED COMPONENTS Core Test)",
                      "set(CMAKE_AUTOMOC ON)",
                      "target_link_libraries(probe PRIVATE Qt6::Core Qt6::Test)",
                      *[f'add_custom_command(TARGET probe POST_BUILD COMMAND ${{CMAKE_COMMAND}} -E copy_if_different $<TARGET_FILE:Qt6::{name}> $<TARGET_FILE_DIR:probe>)'
                        for name in ("Core", "Test")]])
    (directory / "CMakeLists.txt").write_text("\n".join(cmake) + "\n", encoding="utf-8")
    return directory


async def run_isolated_probe(task_id: str, config, directory: Path, *,
                             candidate_include: Path, candidate_library: Path | None = None,
                             candidate_source: Path | None = None,
                             timeout: float = 120,
                             evidence_dir: Path | None = None) -> bool:
    """Compile a fixed probe, then check its entire response in the evaluator."""
    from .isolation import AppContainerDomain, assert_plain_tree

    commands = []
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)

    def record(name, argv, response):
        if evidence_dir is None:
            return
        (evidence_dir / f"probe-{name}.stdout").write_text(response.stdout, encoding="utf-8")
        (evidence_dir / f"probe-{name}.stderr").write_text(response.stderr, encoding="utf-8")
        commands.append({"phase": name, "argv": argv, "status": response.status,
                         "returncode": response.returncode, "duration_ms": response.duration_ms,
                         "stdout_file": f"probe-{name}.stdout",
                         "stderr_file": f"probe-{name}.stderr"})
        (evidence_dir / "probe-commands.json").write_text(
            json.dumps(commands, indent=2), encoding="utf-8")

    source = write_probe_project(task_id, directory / "probe-source",
                                 candidate_include=candidate_include,
                                 candidate_library=candidate_library,
                                 candidate_source=candidate_source)
    build = directory / "probe-build"
    build.mkdir()
    config.validate_for(source, build)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    with AppContainerDomain(config.launcher) as compiler:
        config.grant_read_only(compiler, qt=task_id.startswith("qt-signal-"))
        compiler.grant(source, writable=False)
        compiler.grant(build, writable=True)
        compiler.grant(candidate_include, writable=False)
        if candidate_library is not None:
            compiler.grant(candidate_library.parent, writable=False)
        if candidate_source is not None:
            compiler.grant(candidate_source, writable=False)
        configure_argv = [str(config.cmake), "-S", str(source), "-B", str(build),
            *config.cmake_options(qt=task_id.startswith("qt-signal-"))]
        configured = await compiler.run(configure_argv, build, timeout=timeout, env=config.env)
        record("configure", configure_argv, configured)
        if configured.status != "succeeded":
            return False
        build_argv = [str(config.cmake), "--build", str(build),
            "--config", "Debug", "--target", "probe"]
        compiled = await compiler.run(build_argv, build, timeout=timeout, env=config.env)
        record("build", build_argv, compiled)
        if compiled.status != "succeeded":
            return False
    assert_plain_tree(source)
    assert_plain_tree(build)
    if before != {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}:
        return False
    matches = [p for p in build.rglob("probe.exe") if p.is_file()]
    if len(matches) != 1:
        return False
    challenge = make_challenge(task_id, seed=secrets.randbits(64))
    if evidence_dir is not None:
        (evidence_dir / "probe-challenge.json").write_text(json.dumps({
            "task_id": task_id, "nonce": challenge.nonce,
            "cases": [{"input": case.request, "expected": case.expected}
                      for case in challenge.cases]}, indent=2), encoding="utf-8")
    with AppContainerDomain(config.launcher) as executable:
        config.grant_read_only(executable, qt=task_id.startswith("qt-signal-"))
        executable.grant(build, writable=True)
        execute_argv = [str(matches[0])]
        response = await executable.run(execute_argv, build, timeout=min(timeout, 15),
            env=config.env, input_data=challenge.stdin)
        record("execute", execute_argv, response)
    return (response.status == "succeeded" and not response.truncated
            and verify_response(challenge, returncode=response.returncode,
                                stdout=response.stdout.encode("ascii", errors="replace")))
