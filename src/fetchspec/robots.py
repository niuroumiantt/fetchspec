"""Subset of robots.txt: User-agent groups, Allow/Disallow by longest path, Crawl-delay."""

import re
from urllib.parse import unquote, urlsplit


def _path_of(url):
    parts = urlsplit(url)
    return unquote((parts.path or "/") + ("?" + parts.query if parts.query else ""))


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
        selected, best_len = [], -1
        for group in groups:
            specificity = -1
            for agent in group["agents"]:
                if agent == "*":
                    specificity = max(specificity, 0)
                elif agent and agent in ua:
                    specificity = max(specificity, len(agent))
            if specificity < 0:
                continue
            if specificity > best_len:
                selected, best_len = [group], specificity
            elif specificity == best_len:
                selected.append(group)
        delays = [g["delay"] for g in selected if g["delay"] is not None]
        self.delay = max(delays) if delays else None
        return [rule for group in selected for rule in group["rules"]]

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
            anchored = pattern.endswith("$")
            value = unquote(pattern[:-1] if anchored else pattern)
            expression = "^" + re.escape(value).replace(r"\*", ".*") + ("$" if anchored else "")
            if re.search(expression, path):
                length = len(value.replace("*", "").encode("utf-8"))
                if length > best_len or (length == best_len and allow):
                    best, best_len = allow, length
        return True if best is None else best
