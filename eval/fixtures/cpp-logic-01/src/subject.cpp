#include "subject.h"
int clamp_level(int value) { if (value < 0) return 0; if (value >= 10) return 9; return value; }
