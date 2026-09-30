"""Bounded, plain-text MCP call history; only the reviewed search preset has sources."""
import json
import re
from html import unescape
from urllib.parse import urlsplit

MAX_CALLS = 8
MAX_RECORD_BYTES = 65_536


def plain_excerpt(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Store readable excerpts, never raw HTML pages or active markup.
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", "", unescape(text))
    text = re.sub(r"<[^>]*>", "", text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)[:limit]


def safe_source(title, url):
    if (not isinstance(url, str) or len(url) > 2048 or "[REDACTED]" in url
            or any(c.isspace() or ord(c) < 32 for c in url)):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or "\\" in url
                or any(c in url for c in '<>"') or parsed.port == 0):
            return None
    except ValueError:
        return None
    return {"title": plain_excerpt(title if isinstance(title, str) else url, 256) or url[:256], "url": url}


def search_sources(output):
    """Tavily results JSON/structuredContent or its Title:/URL: text format only.

    No URL scraping of arbitrary tools, HTML, model replies or request arguments.
    Unknown provider formats remain visible as excerpts without inferred sources.
    """
    try:
        result = output if isinstance(output, dict) else json.loads(output)
    except (ValueError, TypeError):
        return []
    if not isinstance(result, dict):
        return []
    candidates = []

    def collect(data):
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            for row in data["results"][:128]:
                if isinstance(row, dict):
                    candidates.append((row.get("title"), row.get("url")))

    collect(result.get("structuredContent"))
    for block in result.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        try:
            collect(json.loads(text))
        except ValueError:
            if 'Detailed Results:' not in text:
                continue
            detail=text.split('Detailed Results:',1)[1].split('\nImages:',1)[0]
            for block in re.split(r'\r?\n\r?\n',detail):
                match=re.match(r'^(?:Title: ([^\r\n]*)\r?\n)?(?:ID: [^\r\n]*\r?\n)?URL: ([^\r\n]+)',block.strip())
                if match:
                    candidates.append(match.groups())
    sources, seen = [], set()
    for title, url in candidates:
        source = safe_source(title, url)
        if source and url not in seen:
            sources.append(source); seen.add(url)
            if len(sources) == 10:
                break
    return sources


def bounded_records(records):
    """Preserve call identities/status before dropping optional excerpts/sources."""
    records = [dict(r) for r in records[:MAX_CALLS]]
    remaining = 10
    for record in records:
        record['sources'] = record.get('sources',[])[:remaining]
        remaining -= len(record['sources'])
    def encode():
        return json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    value = encode()
    if len(value.encode("utf-8")) > MAX_RECORD_BYTES:
        for record in records:
            record.update(arguments_excerpt=record.get("arguments_excerpt", "")[:256],
                          result_excerpt=record.get("result_excerpt", "")[:512], sources=record.get("sources", [])[:1])
        value = encode()
    if len(value.encode("utf-8")) > MAX_RECORD_BYTES:
        for record in records:
            record.update(arguments_excerpt="", result_excerpt="", sources=[])
        value = encode()
    return value
