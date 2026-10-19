import unittest
from browser_automation import consent_reject_ref
from local_browser_companion import CONSENT_REJECT, CONSENT_CONTEXT


class ConsentTests(unittest.TestCase):
    def test_german_consent_dialog(self):
        snapshot='- dialog "Privatsphäre-Einstellungen":\n  - paragraph: Cookies speichern Informationen\n  - button "Alle ablehnen" [ref=e3]\n  - button "Alle akzeptieren" [ref=e4]'
        self.assertEqual(consent_reject_ref(snapshot),'e3')

    def test_no_generic_reject_or_accept_all(self):
        for label in ['Reject','Reject order','Accept all','Alle akzeptieren','Anpassen','Login']:
            self.assertIsNone(CONSENT_REJECT.fullmatch(label))
        self.assertIsNone(consent_reject_ref('- button "Reject all" [ref=e1]\n- paragraph: Cookie policy'))

    def test_no_unrelated_or_ambiguous_dialog(self):
        self.assertIsNone(consent_reject_ref('- dialog "Orders":\n  - button "Reject all" [ref=e2]'))
        self.assertIsNone(consent_reject_ref('- dialog "Cookie preferences":\n  - button "Reject all" [ref=e2]\n  - button "Reject all" [ref=e3]'))
        self.assertTrue(CONSENT_CONTEXT.search('Privacy settings'))


if __name__=='__main__':
    unittest.main()
