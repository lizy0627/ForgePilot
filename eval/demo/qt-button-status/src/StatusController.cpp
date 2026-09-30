#include "StatusController.h"

void StatusController::activate()
{
    if (active_)
        return;
    active_ = true;
}
