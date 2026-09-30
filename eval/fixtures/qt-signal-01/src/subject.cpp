#include "subject.h"
Counter::Counter(QObject* parent) : QObject(parent) {  }
void Counter::click() { emit pressed(); }
void Counter::onPressed() { ++value_; emit changed(value_); }
