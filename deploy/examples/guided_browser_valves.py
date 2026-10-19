"""Replacement Valves class only; this is NOT a complete Open WebUI Function.

Paste this nested class inside the supplied Pipe class, replacing its Valves.
Browser and broker URLs stay blank because those services do not exist on GX10.
Native Hermes gateway port 8642 and bridge port 8643 do not implement /guided/*.

The SEPARATE guided-browser backend must use:
    OpenAI base URL: https://chat.mundusx.ai/v1
    Model: mundusx-agnostic
Its provider key belongs in private backend configuration, never in this file.
"""
from pydantic import BaseModel, Field


class Valves(BaseModel):
    BROWSER_URL: str = Field(
        default='',
        description='Not deployed on GX10. Set only to a real guided-browser service implementing /guided/start and /guided/auto.',
    )
    NOVNC_URL: str = Field(
        default='',
        description='No secure browser takeover URL is configured. Remove the unrelated Mercedes development URL; use an authenticated per-user URL only.',
    )
    BROKER_URL: str = Field(
        default='',
        description='Not deployed on GX10. Requires a broker implementing allocation, /heartbeat and /release with authenticated user isolation.',
    )
    EXEC_MODEL: str = Field(
        default='mundusx-agnostic',
        description='Actual Ehda/MundusX model ID. Configure https://chat.mundusx.ai/v1 and its credential separately on the browser backend.',
    )
    MAX_STEPS: int = Field(default=10, ge=1, le=25, description='Conservative initial autopilot step budget; backend must also enforce time limits.')
    LEGEND_MAX: int = Field(default=25, ge=1, le=100, description='Maximum numbered browser elements displayed.')
