"""Demand-loaded exact-host robots policies for the product pipeline only."""
import hashlib
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, build_opener

from .inventory import InventoryFetcher, PolicyRedirect, USER_AGENT, canonical_url, utc_now
from .robots import Robots


class ProductFetcher(InventoryFetcher):
    """Retain the shared request policy without probing unused allowed hosts.

    Each actual requested or redirected host gets its own robots observation.
    Policy errors are cached for this instance/run and remain fail-closed. Robots
    retrieval itself can redirect only to an allowed host's /robots.txt.
    """
    def __init__(self, profile):
        super().__init__(dict(profile))
        self.policy_failures = {}
        self.policy_receipts = []
        self._loading_robots = False
        self.opener = build_opener(PolicyRedirect(self._redirect_check),
                                   HTTPSHandler(context=ssl.create_default_context()))

    def prepare_robots(self):
        # Compatibility with InventoryFetcher callers; actual hosts are lazy.
        return self.policy_receipts

    def _allowed(self, url):
        parts = urlsplit(canonical_url(url))
        allowed = self.profile.get('allowed_hosts_by_host', {}).get(parts.hostname, self.profile['allowed_hosts'])
        if parts.hostname not in allowed or parts.scheme != 'https' or parts.port not in (None, 443):
            raise ValueError('redirect or URL outside HTTPS host allowlist')
        return parts

    def check(self, url):
        parts = self._allowed(url)  # Never fetch a policy for an untrusted host.
        if self._loading_robots:
            if parts.path != '/robots.txt' or parts.query:
                raise ValueError('robots redirect must remain an allowed /robots.txt URL')
            return
        if parts.path == '/robots.txt' and not parts.query:
            return
        self._ensure_policy(parts.netloc)
        if not self.robots[parts.netloc].allowed(url):
            raise ValueError('robots disallowed')

    def _redirect_check(self, url):
        self.check(url)
        # urllib follows redirects internally, so enforce spacing here as well
        # as at InventoryFetcher.get's initial request boundary.
        policy = self.robots.get(urlsplit(url).netloc)
        delay = max(self.profile['delay_seconds'], (policy.delay or 0) if policy else 0)
        time.sleep(max(0, self.last_request + delay - time.monotonic()))
        self.last_request = time.monotonic()

    def _ensure_policy(self, host):
        if host in self.robots:
            return
        if host in self.policy_failures:
            raise ValueError('robots unavailable for host: ' + host + '; ' + self.policy_failures[host])
        url = 'https://' + host + '/robots.txt'
        original_profile = self.profile
        self.profile = {**original_profile, 'timeout_seconds': min(30, original_profile.get('timeout_seconds', 30)),
                        'max_response_seconds': 30, 'max_xml_bytes': 1024 * 1024}
        self._loading_robots = True
        try:
            try:
                body, meta = super().get(url, cap=1024 * 1024)
            except HTTPError as exc:
                if exc.code not in {404, 410}:
                    raise
                body, meta = b'', {'final_url': url, 'status': exc.code}
            self.robots[host] = Robots(body.decode('utf-8', 'replace'), USER_AGENT)
            self.policy_receipts.append({'url': url, 'observed_at': utc_now(), 'sha256': hashlib.sha256(body).hexdigest(),
                                         'text': body.decode('utf-8', 'replace'), **meta})
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)
            self.policy_failures[host] = error
            self.policy_receipts.append({'url': url, 'observed_at': utc_now(), 'status': 'blocked', 'error': error})
            raise ValueError('robots unavailable for host: ' + host + '; ' + error) from exc
        finally:
            self._loading_robots = False
            self.profile = original_profile
