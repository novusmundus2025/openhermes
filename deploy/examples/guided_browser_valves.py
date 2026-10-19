"""Deprecated partial Valves example.

Use the complete Function in openwebui_guided_browser.py instead:
    python3 deploy/install_openwebui_pipe.py --guided-browser
It uses the existing authenticated bridge, not /guided/*, broker or noVNC services.
"""
from openwebui_guided_browser import Pipe

Valves = Pipe.Valves
