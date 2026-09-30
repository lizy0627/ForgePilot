#include "subject.h"
#include <QCoreApplication>
int main(int argc, char** argv) { QCoreApplication app(argc,argv); Counter c; c.click(); if(c.value()!=1)return 1; c.click(); return c.value()==2?0:1; }
