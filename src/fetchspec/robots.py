"""Subset of robots.txt: User-agent groups, Allow/Disallow by longest path, Crawl-delay."""

from urllib.parse import unquote, urlsplit


def _path_of(url):
    return unquote(urlsplit(url).path or "/")


class Robots:
    def __init__(self, text, user_agent):
        self.delay = None
        self.sitemaps = []
        self._rules = self._select(self._parse(text or ""), user_agent)

    def _parse(self, text):
        groups = []
        current = {"agents": [], "rules": [], "delay": None}
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if current["rules"] or current["delay"] is not None:
                    groups.append(current)
                    current = {"agents": [], "rules": [], "delay": None}
                elif current["agents"] and not current["rules"]:
                    pass
                current["agents"].append(value.lower())
            elif key in ("allow", "disallow") and current["agents"]:
                current["rules"].append((key == "allow", value))
            elif key == "crawl-delay" and current["agents"]:
                try:
                    current["delay"] = float(value)
                except ValueError:
                    pass
            elif key == "sitemap":
                self.sitemaps.append(value)
        if current["agents"]:
            groups.append(current)
        return groups

    def _select(self, groups, user_agent):
        ua = user_agent.lower()
        best, best_len = None, -1
        wildcard = None
        for group in groups:
            for agent in group["agents"]:
                if agent == "*":
                    wildcard = group
                elif ua.startswith(agent) and len(agent) > best_len:
                    best, best_len = group, len(agent)
        chosen = best or wildcard
        if chosen:
            self.delay = chosen.get("delay")
            return chosen["rules"]
        return []

    def allowed(self, url):
        path = _path_of(url)
        best = None
        best_len = -1
        for allow, pattern in self._rules:
            if not pattern:
                if not allow:
                    # Disallow: empty means allow all in this group
                    continue
                pattern = "/"
            if path.startswith(pattern) or (pattern.endswith("$") and path == pattern[:-1]):
                length = len(pattern.rstrip("$"))
                if length > best_len:
                    best, best_len = allow, length
        return True if best is None else best
