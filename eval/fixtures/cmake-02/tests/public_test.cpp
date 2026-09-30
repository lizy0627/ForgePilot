#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(calculate(1)==8); expect(calculate(-7)==0); return failures ? 1 : 0; }
