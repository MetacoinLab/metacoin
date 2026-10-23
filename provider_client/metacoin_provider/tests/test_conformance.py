"""The client's independent wire format and verifier must reproduce every expectation of the versioned fixtures."""
import unittest

from metacoin_provider.conformance import run_fixtures


class ConformanceTests(unittest.TestCase):
    def test_v1_fixtures(self):
        r = run_fixtures('v1'); self.assertTrue(r['ok'], [x for x in r['results'] if not x['ok']]); self.assertGreaterEqual(r['cases'], 20)


if __name__ == '__main__':
    unittest.main()
