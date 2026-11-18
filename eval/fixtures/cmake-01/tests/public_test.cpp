#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(calculate(3)==10); expect(calculate(-2)==-5); return failures ? 1 : 0; }
