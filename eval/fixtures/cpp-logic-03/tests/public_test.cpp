#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(discounted(199,10)==179); expect(discounted(250,20)==200); expect(discounted(99,0)==99); expect(discounted(50,100)==0); return failures ? 1 : 0; }
