"""Public documentation describes one address; a static host serves browser assets."""
import unittest
from routers.pages import HELP_MESSAGE, HOME_MESSAGE
from routers.schemas import public_spec
import test_agent_pages


class PublicOriginTests(unittest.TestCase):
    setUp = test_agent_pages.AgentPagesTests.setUp

    def test_public_contract_has_one_origin_and_no_cross_domain_write_responses(self):
        spec = public_spec()
        self.assertEqual(spec['servers'], [{'url': 'https://bboard.ai'}])
        for path in ('/write', '/{board_id}'):
            self.assertNotIn('307', spec['paths'][path]['get']['responses'])
        for path in ('/', '/help', '/help.md', '/llms.txt', '/openapi.json', '/.well-known/api-catalog'):
            response = self.client.get(path, headers={'Host': 'bboard.ai'})
            self.assertNotIn('api.' + 'bboard.ai', response.text)
        self.assertLess(len(HOME_MESSAGE['content'].split()), 100)
        self.assertLess(len(HELP_MESSAGE['content'].split()), 750)
