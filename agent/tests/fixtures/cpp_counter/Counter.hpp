#pragma once

class Counter {
public:
    void increment();
    int value() const;
private:
    int value_ = 0;
};
