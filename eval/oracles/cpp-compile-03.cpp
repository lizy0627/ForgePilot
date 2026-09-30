#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(math::twice(5) == 10); expect(math::twice(-4) == -8); if(!failures) std::cout << FORGEPILOT_ORACLE_TOKEN << "\n"; return failures ? 1 : 0; }
