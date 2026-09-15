from urllib.parse import urlsplit


def host_allowed(host, allowed_hosts):
    host = (host or "").lower().split(":")[0]
    for allowed in allowed_hosts:
        allowed = allowed.lower()
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def path_blob(url):
    parts = urlsplit(url)
    return parts.path + (("?" + parts.query) if parts.query else "")


def path_allowed(url, rule):
    blob = path_blob(url)
    if rule["_exclude"] and any(p.search(blob) for p in rule["_exclude"]):
        return False
    return any(p.search(blob) for p in rule["_include"])


def keyword_ok(url, text, rule, is_pdf):
    tokens = rule["link_text_include"]
    if not tokens:
        return True
    if is_pdf and path_allowed(url, rule):
        return True
    hay = ((url or "") + " " + (text or "")).lower()
    return any(tok.lower() in hay for tok in tokens)


def route_product_line(url, rule):
    blob = path_blob(url).lower()
    lines = rule["product_lines"]
    scored = []
    for line in lines:
        hints = [h.lower() for h in (line.get("path_hints") or [])]
        score = sum(1 for h in hints if h in blob)
        scored.append((score, line))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored[0][0] > 0:
        return scored[0][1]
    return lines[0]


def looks_pdf(url, content_type=""):
    path = urlsplit(url).path.lower()
    ctype = (content_type or "").lower()
    return path.endswith(".pdf") or "application/pdf" in ctype
