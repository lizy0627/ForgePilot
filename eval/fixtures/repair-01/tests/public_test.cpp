#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(positive_part(-3)==0); expect(positive_part(0)==0); expect(positive_part(8)==8); return failures ? 1 : 0; }
