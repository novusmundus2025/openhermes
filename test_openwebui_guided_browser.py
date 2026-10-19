import asyncio
import unittest
from unittest.mock import patch
from openwebui_guided_browser import Pipe, starting_url


class FunctionTests(unittest.TestCase):
    def test_completed_findings_are_returned_in_chat_instead_of_status(self):
        answer='| GPU | USD / GPU-hour |\n|---|---|\n| Example GPU | $1.25 |\n\nSource: https://example.com/pricing'
        result=Pipe()._message({'auto':{'state':'completed','answer':answer}},'Prices are displayed on the page.')
        self.assertIn(answer,result)
        self.assertNotIn('Prices are displayed on the page.',result)

    def test_unfinished_run_does_not_display_a_stale_answer(self):
        result=Pipe()._message({'auto':{'state':'failed','answer':'stale prices'}},'Provider unavailable.')
        self.assertIn('Provider unavailable.',result)
        self.assertNotIn('stale prices',result)
    def test_browser_reference_is_in_link_not_visible_protocol_comment(self):
        result=Pipe()._message({'view':3,'session_id':'abc123'},'Ready')
        self.assertIn('/guided-browser/#view=3&sid=abc123',result)
        self.assertNotIn('<!--',result)

    def test_google_task_can_start_without_literal_url(self):
        for message in ('Open Google and search for the latest AI news.', 'Check on Google latest AI news top 5', 'Search Google for AI news'):
            self.assertEqual(starting_url(message),'https://www.google.com/')
        self.assertIsNone(starting_url('Summarize my private document'))

    def test_explicit_and_markdown_urls_take_precedence(self):
        self.assertEqual(starting_url('Open [**https://www.cardmarket.com**](https://www.cardmarket.com).'),'https://www.cardmarket.com')
        self.assertEqual(starting_url('Use Google later; first open https://example.com.'),'https://example.com')

    def test_background_function_calls_have_no_browser_side_effects(self):
        async def check():
            pipe=Pipe()
            with patch('pathlib.Path.read_text',side_effect=AssertionError('Background task touched configuration')):
                return [part async for part in pipe.pipe({'messages':[]},{'id':'alice','role':'user'},__task__='title_generation')]
        self.assertEqual(asyncio.run(check()),[''])
