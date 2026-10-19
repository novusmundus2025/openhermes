# Dedicated local browser

Ehda inference and account ownership remain on GX10. The Windows companion polls
Open WebUI for commands and runs a separate, visible Chrome or Edge profile on
the user's computer. No inbound port, remote-debugging listener, personal browser
profile, Open WebUI password, or account JWT is required.

On the Guided Browser page, stop any current browser session, choose **Pair this
computer**, and start the companion. Enter the five-minute pairing code at its
prompt. Pairing selects the local transport; **Use GX10 browser** switches back
after stopping the current session. **Disconnect local browser** revokes pairing.
Each account has one paired device. Pairing credentials are held in companion
memory and only SHA-256 hashes are stored server-side. Restarting the companion
requires pairing again. Server restart preserves pairing and selected transport.

Install Python and Chrome or Edge, then in a dedicated directory:

```powershell
python -m venv .openhermes-browser
.openhermes-browser\Scripts\python -m pip install playwright==1.55.0
.openhermes-browser\Scripts\python local_browser_companion.py --server http://100.124.143.79:3000
```

Use `--gui` with `pythonw.exe` for a pairing dialog without a terminal. The
currently provisioned computer also has an **OpenHermes Browser** desktop shortcut
and a private runtime under `%LOCALAPPDATA%\OpenHermes`.

Pause AI and wait for **Human control** before interacting with the local window,
including login or verification. Refresh or resume in Open WebUI after manual
navigation. A pending action finishes before human handoff. Input values are
excluded from local snapshots and input regions are masked in screenshots. The
model sees website text; keep unrelated private pages out of the dedicated window.

Temporary browser profiles live under `%LOCALAPPDATA%\OpenHermes\browser-sessions`
and are cleared on stop, disconnect, normal companion exit, or command timeout.
A hard process kill or power failure can leave files behind; this is not a secure
erasure guarantee. Sessions expire after 15 minutes of inactivity.

The companion uses `/api/openhermes/browser/local` for scoped pairing/poll/result
requests. Only authenticated Open WebUI users can issue commands or pairing codes;
devices cannot initiate commands. Private website addresses are rejected both
server-side and on local browser HTTP requests. This validation is defense in
depth, not an OS network sandbox. Use HTTPS for public servers; HTTP is accepted
only for a trusted private/LAN/Tailscale address.

Local browsing does not guarantee access to sites that restrict automation. Otto's
search page was blank in the initial local test as well. Security-check and empty
pages stop AI with an access explanation rather than an invented result or login
claim. No CAPTCHA bypass, proxy rotation, or fingerprint spoofing is included.

Deploy `local_browser.py` alongside the bridge; deploy the router, companion and
Guided Browser assets into `/app/backend/data/openhermes-projects/` in Open WebUI.
Restart both services while idle when Python code changes. The companion is an
explicit download from `/guided-browser/local-companion.py`.

Regression tests: `python -m unittest test_local_browser test_guided_browser test_browser_automation`.
