#include "subject.h"
#include <iostream>
static int failures=0;
static void expect(bool ok) { if(!ok){ ++failures; std::cerr << "expectation failed\n"; } }
int main() { expect(!leap_year(1900)); expect(leap_year(2000)); expect(leap_year(2024)); expect(!leap_year(2023)); expect(!leap_year(2100)); return failures ? 1 : 0; }
