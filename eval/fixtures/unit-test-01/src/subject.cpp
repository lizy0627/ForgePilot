#include "subject.h"
int parse_count(const std::string& text) { try { return std::stoi(text); } catch (...) { return -1; } }
