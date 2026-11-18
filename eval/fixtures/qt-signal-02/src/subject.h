#pragma once
#include <QObject>
class Counter : public QObject {
 Q_OBJECT
public:
 explicit Counter(QObject* parent=nullptr);
 int value() const { return value_; }
 void click();
signals:
 void pressed();
 void changed(int value);
private slots:
 void onPressed();
private:
 int value_=0;
};
