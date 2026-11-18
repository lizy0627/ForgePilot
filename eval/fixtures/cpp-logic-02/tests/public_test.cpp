#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(max_of_three(-9,-2,-5)==-2); expect(max_of_three(4,2,3)==4); expect(max_of_three(-1,-1,-1)==-1); return failures ? 1 : 0; }
