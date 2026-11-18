#include "subject.h"
Counter::Counter(QObject* parent) : QObject(parent) { connect(this, &Counter::pressed, this, &Counter::onPressed); connect(this, &Counter::pressed, this, &Counter::onPressed); }
void Counter::click() { emit pressed(); }
void Counter::onPressed() { ++value_; emit changed(value_); }
