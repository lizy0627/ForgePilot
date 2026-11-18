#pragma once
#include "counter.h"
class Controller { Counter counter_; public: void increment(); };
void onClicked(Controller& controller);
