"""Prepare an independent repair candidate for ordered parsed arguments."""

from pathlib import Path
import os
import shutil

temp = Path(os.environ["TEMP"])
source = temp / "forgepilot-audit-real-feature-01-20261104"
target = temp / "forgepilot-repaired-real-feature-01-20261104"
shutil.copytree(source, target)
header = target / "include/cxxopts.hpp"
text = header.read_text(encoding="utf-8")
marker = "  class ParseResult\n  {"
assert text.count(marker) == 1
text = text.replace(marker, '''  class ParsedArgument
  {
    public:
    ParsedArgument(std::string key, std::string value)
    : m_key(std::move(key)), m_value(std::move(value)) {}

    const std::string& key() const { return m_key; }
    const std::string& value() const { return m_value; }

    template <typename T>
    T as() const
    {
      T result{};
      values::parse_value(m_value, result);
      return result;
    }

    private:
    std::string m_key;
    std::string m_value;
  };

'''+marker)
marker = "    private:\n\n    OptionValue&\n    get_option"
assert text.count(marker) == 1
text = text.replace(marker, '''    const std::vector<ParsedArgument>&
    arguments() const
    {
      return m_arguments;
    }

'''+marker)
marker = "    std::unordered_map<std::shared_ptr<OptionDetails>, OptionValue> m_results;"
assert text.count(marker) == 1
text = text.replace(marker, marker + "\n    std::vector<ParsedArgument> m_arguments;")
marker = "  result.parse(value, arg);\n}"
assert text.count(marker) == 1
text = text.replace(marker, '''  result.parse(value, arg);
  const auto& key = value->long_name().empty() ? value->short_name() : value->long_name();
  m_arguments.emplace_back(key, arg);
}''')
header.write_text(text, encoding="utf-8")
test = target / "test/options.cpp"
with test.open("a", encoding="utf-8") as out:
    out.write('''
TEST_CASE("Parsed arguments retain command-line order", "[options]") {
  cxxopts::Options options("order", "ordered arguments");
  options.add_options()
    ("value", "Number", cxxopts::value<std::vector<int>>())
    ("flag", "Flag");
  Argv av({"order", "--value", "3", "--flag", "--value", "7"});
  auto** argv = av.argv();
  auto argc = av.argc();
  auto result = options.parse(argc, argv);
  const auto& arguments = result.arguments();
  REQUIRE(arguments.size() == 3);
  CHECK(arguments[0].key() == "value");
  CHECK(arguments[0].value() == "3");
  CHECK(arguments[0].as<int>() == 3);
  CHECK(arguments[1].key() == "flag");
  CHECK(arguments[1].as<bool>());
  CHECK(arguments[2].key() == "value");
  CHECK(arguments[2].value() == "7");
  CHECK(arguments[2].as<int>() == 7);
  CHECK((result["value"].as<std::vector<int>>() == std::vector<int>{3, 7}));
}
''')
print(target)
