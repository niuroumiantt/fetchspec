import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from fetchspec.network import ProductFetcher


class Response:
    def __init__(self, url, body):
        self.url, self.fp, self.status = url, io.BytesIO(body), 200
        self.headers = {'Content-Type': 'text/plain', 'Content-Length': str(len(body))}
    def geturl(self):
        return self.url
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, count):
        return self.fp.read(count)


class Opener:
    def __init__(self, fetcher, responses):
        self.fetcher, self.responses, self.calls = fetcher, responses, []
        self.budgets = []
    def open(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        self.budgets.append((url, timeout, self.fetcher.profile['max_response_seconds']))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            target, body = response
            self.fetcher._redirect_check(target)
            self.calls.append(target)
            return Response(target, body)
        return Response(url, response)


class ProductNetworkTests(unittest.TestCase):
    def make(self, responses):
        fetcher = ProductFetcher({'allowed_hosts': ['a.test', 'b.test', 'unused.test'],
                                  'delay_seconds': 0, 'timeout_seconds': 50,
                                  'max_xml_bytes': 20*1024*1024, 'max_response_seconds': 600})
        fetcher.opener = Opener(fetcher, responses)
        return fetcher

    def test_only_actual_host_policy_precedes_body_with_small_budgets(self):
        fetcher = self.make({'https://a.test/robots.txt': b'User-agent: *\nAllow: /', 'https://a.test/product': b'body'})
        self.assertEqual(fetcher.prepare_robots(), [])
        self.assertEqual(fetcher.opener.calls, [])
        fetcher.get('https://a.test/product')
        self.assertEqual(fetcher.opener.calls, ['https://a.test/robots.txt','https://a.test/product'])
        self.assertEqual(fetcher.opener.budgets[0][1:], (30,30))
        self.assertEqual(fetcher.opener.budgets[1][1:], (50,600))
        fetcher.get('https://a.test/product')
        self.assertEqual(fetcher.opener.calls.count('https://a.test/robots.txt'),1)

    def test_redirect_host_gets_own_policy_before_redirected_body(self):
        fetcher = self.make({'https://a.test/robots.txt': b'User-agent: *\nAllow: /',
                             'https://b.test/robots.txt': b'User-agent: *\nAllow: /',
                             'https://a.test/product': ('https://b.test/product', b'body')})
        fetcher.get('https://a.test/product')
        self.assertEqual(fetcher.opener.calls, ['https://a.test/robots.txt','https://a.test/product','https://b.test/robots.txt','https://b.test/product'])

    def test_missing_robots_allows_but_503_is_cached_fail_closed(self):
        for status in (404,410):
            fetcher = self.make({'https://a.test/robots.txt': HTTPError('https://a.test/robots.txt', status, 'missing', {}, None), 'https://a.test/product': b'body'})
            self.assertEqual(fetcher.get('https://a.test/product')[0], b'body')
        fetcher = self.make({'https://a.test/robots.txt': HTTPError('https://a.test/robots.txt', 503, 'unavailable', {}, None)})
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'robots unavailable'):
                fetcher.get('https://a.test/product')
        self.assertEqual(fetcher.opener.calls, ['https://a.test/robots.txt'])

    def test_allowlist_and_robots_disallow_fail_before_body(self):
        fetcher = self.make({'https://a.test/robots.txt': b'User-agent: *\nDisallow: /'})
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            fetcher.get('https://evil.test/product')
        self.assertEqual(fetcher.opener.calls, [])
        with self.assertRaisesRegex(ValueError, 'robots disallowed'):
            fetcher.get('https://a.test/product')
        self.assertEqual(fetcher.opener.calls, ['https://a.test/robots.txt'])

    def test_robots_redirect_cannot_load_an_arbitrary_page(self):
        fetcher = self.make({'https://a.test/robots.txt': ('https://b.test/login', b'not robots')})
        with self.assertRaisesRegex(ValueError, 'robots redirect'):
            fetcher.get('https://a.test/product')
        self.assertEqual(fetcher.opener.calls, ['https://a.test/robots.txt'])

    def test_reviewed_locale_robots_redirect_is_exact_and_keeps_disallow(self):
        fetcher = self.make({'https://a.test/robots.txt': ('https://a.test/en/robots.txt', b'User-agent: *\nDisallow: /private'), 'https://a.test/product': b'body'})
        fetcher.profile['robots_redirect_paths'] = {'a.test': ['/en/robots.txt']}
        self.assertEqual(fetcher.get('https://a.test/product')[0], b'body')
        self.assertEqual(fetcher.policy_receipts[0]['final_url'], 'https://a.test/en/robots.txt')
        with self.assertRaisesRegex(ValueError, 'robots disallowed'):
            fetcher.get('https://a.test/private')
        fetcher._loading_robots = True
        for url in ['https://a.test/en/robots.txt?x=1','https://b.test/en/robots.txt','https://a.test/en/login']:
            with self.assertRaisesRegex(ValueError, 'robots redirect'): fetcher.check(url)

    def test_robot_size_limit_is_one_mebibyte(self):
        fetcher = self.make({'https://a.test/robots.txt': b'#'*(1024*1024+1)})
        with self.assertRaisesRegex(ValueError, 'response exceeds limit'):
            fetcher.get('https://a.test/product')
        self.assertEqual(fetcher.opener.calls, ['https://a.test/robots.txt'])


if __name__ == '__main__':
    unittest.main()
