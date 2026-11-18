from pathlib import Path
import shutil
import tempfile

from common import command, guarded, repository_argument


def check(args):
    cmake = shutil.which(args.cmake)
    if not cmake:
        return "tool_missing"
    with tempfile.TemporaryDirectory(prefix="forgepilot-protected-oracle-") as scratch_name:
        scratch = Path(scratch_name).resolve()
        if scratch.is_relative_to(args.repository) or args.repository.is_relative_to(scratch):
            return "isolation_failed"
        prefix = scratch / "install"
        cases = [
            ("default", None, "${prefix}/include"),
            ("relative", "include/custom", "${prefix}/include/custom"),
            ("absolute", (scratch / "absolute-headers").as_posix(),
             (scratch / "absolute-headers").as_posix()),
        ]
        for name, include_dir, expected in cases:
            build = scratch / ("build-" + name)
            argv = [cmake, "-S", str(args.repository), "-B", str(build),
                    "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DCXXOPTS_BUILD_TESTS=OFF",
                    "-DCXXOPTS_BUILD_EXAMPLES=OFF", "-DCXXOPTS_USE_UNICODE_HELP=OFF",
                    f"-DCMAKE_INSTALL_PREFIX={prefix.as_posix()}"]
            if include_dir is not None:
                argv.append(f"-DCMAKE_INSTALL_INCLUDEDIR={include_dir}")
            if args.generator:
                argv.extend(["-G", args.generator])
            if not command(argv, scratch):
                return "configure_failed"
            package_files = list(build.rglob("cxxopts.pc"))
            if len(package_files) != 1:
                return "package_missing"
            includedirs = [line.split("=", 1)[1] for line in package_files[0].read_text(encoding="utf-8").splitlines()
                           if line.startswith("includedir=")]
            if includedirs != [expected]:
                return "incorrect_include_path"
    return "passed"


if __name__ == "__main__":
    raise SystemExit(guarded(lambda: check(repository_argument())))
