from common import guarded, repository_argument, run_cpp_oracle


SOURCE = r'''
#include <cxxopts.hpp>
#include <string>
bool accepts(const char* value) {
  cxxopts::Options options("probe", "probe");
  options.add_options()("float", "Floating point", cxxopts::value<float>());
  char a0[] = "probe", a1[] = "--float";
  char* argv[] = {a0, a1, const_cast<char*>(value)};
  int argc = 3;
  try { options.parse(argc, argv); return true; }
  catch (const cxxopts::exceptions::incorrect_argument_type&) { return false; }
}
int main() {
  return accepts("2.5") && accepts("nan") && !accepts("2.5typo") && !accepts("nanXYZ") ? 0 : 1;
}
'''


if __name__ == "__main__":
    raise SystemExit(guarded(lambda: run_cpp_oracle(repository_argument(), SOURCE)))
