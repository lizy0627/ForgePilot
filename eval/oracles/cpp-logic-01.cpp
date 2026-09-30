#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { for (int n=-12; n<=22; ++n) expect(clamp_level(n) == (n<0?0:n>10?10:n)); if(!failures) std::cout << FORGEPILOT_ORACLE_TOKEN << "\n"; return failures ? 1 : 0; }
