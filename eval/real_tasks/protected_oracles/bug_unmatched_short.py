from common import guarded, repository_argument, run_cpp_oracle


SOURCE = r'''
#include <cxxopts.hpp>
#include <string>
#include <vector>
int main() {
  cxxopts::Options options("probe", "probe");
  options.allow_unrecognised_options();
  char a0[] = "probe", a1[] = "-z", a2[] = "--mystery", a3[] = "-q";
  char* argv[] = {a0, a1, a2, a3};
  int argc = 4;
  auto result = options.parse(argc, argv);
  return result.unmatched() == std::vector<std::string>{"-z", "--mystery", "-q"} ? 0 : 1;
}
'''


if __name__ == "__main__":
    raise SystemExit(guarded(lambda: run_cpp_oracle(repository_argument(), SOURCE)))
