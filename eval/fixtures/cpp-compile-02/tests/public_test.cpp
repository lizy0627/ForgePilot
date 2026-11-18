#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(square(4) == 16); expect(square(-3) == 9); expect(square(0) == 0); return failures ? 1 : 0; }
