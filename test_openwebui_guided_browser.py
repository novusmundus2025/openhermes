import asyncio
import unittest
from unittest.mock import patch
from openwebui_guided_browser import Pipe


class FunctionTests(unittest.TestCase):
    def test_background_function_calls_have_no_browser_side_effects(self):
        async def check():
            pipe=Pipe()
            with patch('pathlib.Path.read_text',side_effect=AssertionError('Background task touched configuration')):
                return [part async for part in pipe.pipe({'messages':[]},{'id':'alice','role':'user'},__task__='title_generation')]
        self.assertEqual(asyncio.run(check()),[''])
