"""
title: Hermes User Profiles
description: Authenticated, automatic per-user Hermes profiles.
version: 1.0.0
"""
import json
from pathlib import Path
import aiohttp


class Pipe:
    def __init__(self):
        self.name = "Hermes User Profiles"

    async def pipe(self, body: dict, __user__: dict):
        # __user__ is injected by Open WebUI after authentication, never read from body.
        if not __user__ or not __user__.get("id") or __user__.get("role") not in ("user", "admin"):
            raise ValueError("An approved Open WebUI account is required")
        config = json.loads(Path("/app/backend/data/openhermes-bridge.json").read_text())
        user = {k: __user__.get(k, "") for k in ("id", "name", "email")}
        messages = body.get('messages', [])
        account = bool(messages and messages[-1].get('role') == 'user'
                       and messages[-1].get('content') == '/credits')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as session:
            async with session.post(config["url"] + ("/account" if account else "/chat"),
                headers={"Authorization": "Bearer " + config["key"]},
                json={"user": user, "body": body}) as response:
                if response.status != 200:
                    raise RuntimeError("Hermes profile service returned " + str(response.status) + ": " + (await response.text())[:300])
                if account:
                    data = await response.json()
                    def amount(key):
                        return format(data[key] / 1_000_000, '.6f').rstrip('0').rstrip('.') or '0'
                    yield ('Remaining credits: ' + amount('balance_microunits') + '\n\n'
                           + 'Credits earned from contributions: ' + amount('earned_microunits') + '\n\n'
                           + 'Credits granted: ' + amount('granted_microunits') + '\n\n'
                           + 'Credits spent: ' + amount('spent_microunits') + '\n\n'
                           + ('Pricing configured' if data['pricing_configured'] else 'Usage pricing is not configured yet') + '\n\n'
                           + 'Recorded requests: ' + str(data['requests']) + '\n\n'
                           + 'Input tokens: ' + str(data['prompt_tokens'] or 0) + '\n\n'
                           + 'Output tokens: ' + str(data['completion_tokens'] or 0) + '\n\n'
                           + 'Requests charged using character estimates: ' + str(data['estimated_requests']) + '\n\n'
                           + 'Requests awaiting credit calculation: ' + str(data['unrated_requests'] or 0))
                    return
                if body.get("stream"):
                    async for line in response.content:
                        line = line.decode("utf-8").strip()
                        if line.startswith("data:") and line != "data: [DONE]":
                            yield line + "\n\n"
                else:
                    data = await response.json()
                    yield data["choices"][0]["message"].get("content", "")
