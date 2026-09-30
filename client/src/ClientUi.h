#pragma once
#include <QGuiApplication>
#include <QQuickStyle>
#include <QTranslator>
#include <QFont>

// Used by the product and its UI tests so built-in dialogs have the same locale/style.
inline void configureClientUi(QGuiApplication &app)
{
    QQuickStyle::setStyle("Fusion");
    QFont font("Microsoft YaHei UI");
    font.setPixelSize(13);
    app.setFont(font);
    for (const auto *name : {"qtbase_zh_CN", "qtdeclarative_zh_CN"}) {
        auto *translator = new QTranslator(&app);
        if (translator->load(QString(":/translations/%1.qm").arg(name)))
            app.installTranslator(translator);
    }
}
