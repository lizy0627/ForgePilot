#include "Counter.hpp"

class App {
public:
    int onClicked() {
        counter_.increment();
        return counter_.value();
    }
private:
    Counter counter_;
};

int runApp() {
    App app;
    return app.onClicked();
}
