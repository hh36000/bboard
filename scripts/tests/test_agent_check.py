"""The verification script must distinguish real failures from cached scores."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_agent_readiness import Observation, PageText, check


class AgentCheckTests(unittest.TestCase):
    def test_visible_text_ignores_scripts_and_style(self):
        self.assertEqual(PageText('<html><head><title>hidden</title></head><body><script>hidden</script><style>hidden</style><h1>Visible</h1> text</body></html>').text, 'Visible text')

    def test_soft_404s_and_html_shells_cannot_pass_discovery_checks(self):
        with patch('check_agent_readiness.fetch', return_value=Observation(200, {'content-type': 'text/html'}, '<html><body><div id="root"></div></body></html>')) as fetch:
            report = check('https://site.test')
        failures = {item['check'] for item in report['checks'] if not item['passed']}
        self.assertTrue({'Readable homepage', 'Real frontend 404', 'API catalog', 'Self-contained OpenAPI', 'Rate-limit metadata'} <= failures)
        self.assertEqual(report['total'], 19)
        self.assertEqual(fetch.call_count, 19)

    def test_network_errors_are_reported_without_claiming_success(self):
        with patch('check_agent_readiness.fetch', side_effect=OSError('unavailable')):
            report = check('https://site.test')
        self.assertEqual(report['passed'], 0)
        self.assertTrue(all(item['error'] == 'unavailable' for item in report['checks']))
