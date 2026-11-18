#pragma once

#include <QColor>
#include <algorithm>
#include <cmath>

inline double colorContrast(const QColor &foreground, const QColor &background)
{
    const auto luminance = [](const QColor &color) {
        const auto channel = [](int value) {
            const double normalized = value / 255.0;
            return normalized <= 0.04045 ? normalized / 12.92
                : std::pow((normalized + 0.055) / 1.055, 2.4);
        };
        return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green())
            + 0.0722 * channel(color.blue());
    };
    const double a = luminance(foreground);
    const double b = luminance(background);
    return (std::max(a, b) + 0.05) / (std::min(a, b) + 0.05);
}
