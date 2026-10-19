"""One isolated native Hermes browser runtime, JSON-line input/output, no AI calls."""
import base64
import json
import logging
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time


def main():
    output = sys.stdout
    sys.stdout = sys.stderr
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, '/home/a/.hermes/hermes-agent')
    import hermes_bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tools import browser_tool as browser
    from tools.browser_tool_lifecycle import cleanup_browser
    from tools.terminal_tool import _atexit_cleanup
    from guided_browser import public_url
    task = 'guided-'+Path(os.environ['HERMES_HOME']).name
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parent = os.getppid()
    def watch_parent():
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os.kill(os.getpid(), signal.SIGTERM)
                return
    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        for line in sys.stdin:
            try:
                body = json.loads(line)
                action = body['action']
                if action == 'navigate':
                    result = json.loads(browser.browser_navigate(body['url'], task_id=task))
                elif action == 'click':
                    result = json.loads(browser.browser_click(body['ref'], task_id=task))
                elif action == 'type':
                    result = json.loads(browser.browser_type(body['ref'], body['text'], task_id=task))
                elif action == 'back':
                    result = json.loads(browser.browser_back(task_id=task))
                elif action == 'scroll':
                    result = json.loads(browser.browser_scroll(body['direction'], task_id=task))
                else:
                    result = {'success': True}
                if not result.get('success'):
                    raise ValueError('Native browser operation failed')
                key = browser._last_session_key(task)
                if action in ('navigate', 'click', 'back'):
                    browser._session._run_browser_command(key, 'wait', ['--load', 'domcontentloaded'], timeout=15)
                current = browser._session._run_browser_command(key, 'get', ['url'])
                raw = current.get('data') or {}
                url = raw.get('url') or raw.get('value') or raw.get('result') or result.get('url')
                if not isinstance(url,str) or not url.startswith(('https://','http://')):
                    response={'success':False,'error':'The browser did not reach a website. Navigation failed or the browser session reset before its page could be read. No login or inbox access occurred.'}
                    output.write(json.dumps(response)+'\n');output.flush()
                    continue
                public_url(url)
                view = json.loads(browser.browser_snapshot(full=True, task_id=task))
                if not view.get('success'):
                    raise ValueError('Native snapshot failed')
                image = None
                warning = None
                from browser_automation import site_blocked
                if site_blocked({'title':view.get('title') or result.get('title',''),'snapshot':view.get('snapshot','')}):
                    warning="The website's security service blocked this server browser. Login and inbox access are unavailable on this page."
                with tempfile.TemporaryDirectory(prefix='openhermes-view-') as directory:
                    _, screenshot, error = browser._capture_vision_screenshot(key, True, Path(directory)/'view.png', False)
                    if not error and screenshot.is_file() and screenshot.stat().st_size <= 6*1024*1024:
                        image = 'data:image/png;base64,' + base64.b64encode(screenshot.read_bytes()).decode()
                    else:
                        warning = (warning+' ' if warning else '')+'Screenshot unavailable; use the element snapshot.'
                response = {'success': True, 'url': url, 'title': view.get('title') or result.get('title', ''),
                            'snapshot': view.get('snapshot', ''), 'image': image, 'warning': warning}
            except Exception:
                # No raw text, URLs, credentials or native exception details in logs/history.
                response = {'success': False, 'error': 'Native browser operation failed. Refresh or restart the session.'}
            output.write(json.dumps(response)+'\n')
            output.flush()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_browser(task)
        _atexit_cleanup()


if __name__ == '__main__':
    main()
