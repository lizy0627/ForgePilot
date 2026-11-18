#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(parse_count("12x")==-1); expect(parse_count("-1")==-1); expect(parse_count(" 3")==-1); expect(parse_count("")==-1); expect(parse_count("012")==12); expect(parse_count("2147483648")==-1); expect(parse_count("0")==0); if(!failures) std::cout << FORGEPILOT_ORACLE_TOKEN << "\n"; return failures ? 1 : 0; }
