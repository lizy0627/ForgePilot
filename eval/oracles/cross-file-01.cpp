#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(order_total(3,4)==12); expect(order_total(7,0)==0); expect(subtotal(9,2)==18); if(!failures) std::cout << FORGEPILOT_ORACLE_TOKEN << "\n"; return failures ? 1 : 0; }
