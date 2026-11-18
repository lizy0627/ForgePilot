#include "Counter.hpp"

void Counter::increment() {
    ++value_;
}

int Counter::value() const {
    return value_;
}
