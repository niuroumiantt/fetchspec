import tempfile
import unittest
from pathlib import Path

from fetchspec.product_map import acknowledge_map, sitemap_profile, sync_map


class SitemapFetcher:
    def __init__(self, urls, broken=False):
        self.urls, self.broken = urls, broken
        self.calls = []

    def prepare_robots(self):
        return []

    def get(self, url, **kwargs):
        self.calls.append(url)
        profile = sitemap_profile('supermicro')
        if url == profile['sitemap_index']:
            body = '<sitemapindex>' + ''.join('<sitemap><loc>'+s['url']+'</loc></sitemap>' for s in profile['sitemaps']) + '</sitemapindex>'
        elif url == profile['sitemaps'][0]['url']:
            body = '<urlset>' + ''.join('<url><loc>'+u+'</loc><lastmod>'+m+'</lastmod></url>' for u,m in self.urls) + '</urlset>'
        elif self.broken:
            body = '<urlset><malformed'
        else:
            body = '<urlset><url><loc>https://www.supermicro.com/support/not-a-product</loc></url></urlset>'
        return body.encode(), {'final_url': url, 'status': 200, 'content_type': 'application/xml'}


class ProductMapTests(unittest.TestCase):
    def test_new_unchanged_changed_missing_and_migrated_baseline(self):
        a = 'https://www.supermicro.com/en/products/system/sys-a'
        b = 'https://www.supermicro.com/en/products/system/sys-b'
        foreign = 'https://www.supermicro.com/ja-jp/products/system/sys-a'
        news = 'https://www.supermicro.com/en/pressreleases/latest'
        with tempfile.TemporaryDirectory() as directory:
            known = {'products': [{'source_url': a}]}
            first = sync_map(directory, 'supermicro', fetcher=SitemapFetcher([(a,'2026-09-01'),(b,'2026-09-01'),(foreign,'x'),(news,'x')]), known_catalog=known)
            self.assertEqual(first['counts'], {'new':1,'unchanged':1,'changed':0,'missing_review':0})
            self.assertEqual([p['url'] for p in first['plan']], [b])
            second = sync_map(directory, 'supermicro', fetcher=SitemapFetcher([(a,'2026-09-01'),(b,'2026-09-01')]))
            self.assertEqual([p['url'] for p in second['plan']], [b])
            self.assertTrue(second['plan'][0]['pending_until_reviewed_or_collected'])
            self.assertEqual(acknowledge_map(directory, 'supermicro', [b])['acknowledged'], 1)
            changed = sync_map(directory, 'supermicro', fetcher=SitemapFetcher([(a,'2026-09-02')]))
            self.assertEqual(changed['counts']['changed'],1)
            self.assertEqual(changed['counts']['missing_review'],1)
            self.assertEqual(changed['products_retired'],0)
            self.assertTrue((Path(directory)/'acquisition/supermicro/map-plan.json').is_file())

    def test_incomplete_sitemap_never_marks_missing(self):
        a = 'https://www.supermicro.com/en/products/system/sys-a'
        with tempfile.TemporaryDirectory() as directory:
            sync_map(directory, 'supermicro', fetcher=SitemapFetcher([(a,'2026-09-01')]))
            broken = sync_map(directory, 'supermicro', fetcher=SitemapFetcher([], broken=True))
            self.assertFalse(broken['sitemaps_complete'])
            self.assertEqual(broken['counts']['missing_review'],0)
            self.assertTrue(broken['errors'])
            recovered = sync_map(directory, 'supermicro', fetcher=SitemapFetcher([(a,'2026-09-01')]))
            self.assertEqual(recovered['counts']['unchanged'],1)

    def test_profile_only_selects_product_sitemaps(self):
        profile = sitemap_profile('supermicro')
        self.assertEqual({s['role'] for s in profile['sitemaps']}, {'system','chassis','motherboard','accessories'})
        nvidia = sitemap_profile('nvidia')
        self.assertEqual([s['role'] for s in nvidia['sitemaps']], ['en_us'])
        self.assertEqual(len(nvidia['sitemap_index_select']),2)


if __name__ == '__main__':
    unittest.main()
