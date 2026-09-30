#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(add(2, 3) == 5); expect(add(-7, 4) == -3); expect(add(0, 0) == 0); if(!failures) std::cout << FORGEPILOT_ORACLE_TOKEN << "\n"; return failures ? 1 : 0; }
