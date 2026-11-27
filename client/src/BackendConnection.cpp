#include "BackendConnection.h"
#include "Protocol.h"

bool BackendConnection::configureBackend(const QString &address)
{
    return setBaseUrl(QUrl(address));
}

bool BackendConnection::setBaseUrl(const QUrl &url)
{
    if (!Protocol::validBaseUrl(url)) {
        if (m_error.isEmpty()) {
            m_error = QStringLiteral("请使用本机 HTTP 地址，不包含凭据、路径、查询参数或片段。");
            emit errorChanged();
        }
        return false;
    }
    if (!m_error.isEmpty()) {
        m_error.clear();
        emit errorChanged();
    }
    if (url != m_baseUrl) {
        m_baseUrl = url;
        emit baseUrlChanged();
    }
    return true;
}
