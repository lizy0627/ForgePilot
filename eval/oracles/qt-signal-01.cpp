#include "subject.h"
#include <QCoreApplication>
#include <QSignalSpy>
#include <iostream>
int main(int argc, char** argv) { QCoreApplication app(argc,argv); Counter c; QSignalSpy spy(&c,&Counter::changed); if(!spy.isValid())return 2; for(int n=1;n<=5;++n){c.click(); if(c.value()!=n || spy.count()!=n || spy.at(n-1).at(0).toInt()!=n)return 1;} std::cout << FORGEPILOT_ORACLE_TOKEN << "\n"; return 0; }
