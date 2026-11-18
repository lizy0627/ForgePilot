#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(order_total(3,4)==12); expect(order_total(7,0)==0); return failures ? 1 : 0; }
