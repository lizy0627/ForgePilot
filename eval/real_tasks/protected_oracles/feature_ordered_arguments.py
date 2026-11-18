from common import guarded, repository_argument, run_cpp_oracle


SOURCE = r'''
#include <cxxopts.hpp>
#include <string>
int main() {
  cxxopts::Options options("probe", "probe");
  options.add_options()
    ("v,value", "A value", cxxopts::value<std::string>())
    ("f,flag", "A flag");
  char a0[] = "probe", a1[] = "--value", a2[] = "one";
  char a3[] = "--flag", a4[] = "-v", a5[] = "two";
  char* argv[] = {a0, a1, a2, a3, a4, a5};
  char** actual_argv = argv;
  int argc = 6;
  auto result = options.parse(argc, actual_argv);
  const auto& items = result.arguments();
  return items.size() == 3 &&
    items[0].key() == "value" && items[0].value() == "one" &&
    items[1].key() == "flag" && items[1].as<bool>() &&
    items[2].key() == "value" && items[2].value() == "two" ? 0 : 1;
}
'''


if __name__ == "__main__":
    raise SystemExit(guarded(lambda: run_cpp_oracle(repository_argument(), SOURCE)))
