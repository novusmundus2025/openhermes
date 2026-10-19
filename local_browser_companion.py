"""OpenHermes dedicated local browser. Install: python -m pip install playwright==1.55.0

Run: python local_browser_companion.py --server http://100.124.143.79:3000
Uses installed Chrome/Edge; pairing code is entered privately at the prompt.
No listening port, personal browser profile, account password, or Open WebUI token.
"""
import argparse
import base64
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def public_url(value):
    url = urlsplit(value)
    if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password or url.port not in (None,80,443):
        raise ValueError('Only public HTTP(S) websites are supported')
    addresses = socket.getaddrinfo(url.hostname, url.port or (443 if url.scheme=='https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Private network websites are not supported')
    return value


CONSENT_REJECT = re.compile(r'^(?:alle ablehnen|alles ablehnen|reject all|reject optional cookies|reject non-essential cookies|decline all|nur notwendige cookies|only necessary cookies|necessary cookies only|tout refuser|odrzuć wszystkie)$', re.I)
CONSENT_CONTEXT = re.compile(r'cookie|consent|privatsphäre|privacy settings|datenschutz|confidentialité|prywatno', re.I)


def reject_cookie_dialog(page):
    try:
        return _reject_cookie_dialog(page)
    except Exception:
        # A disappearing banner/frame must not fail an otherwise successful page read.
        return False


def _reject_cookie_dialog(page):
    """Exact reject buttons, inside consent dialogs only. Never accept-all or generic reject."""
    for frame in page.frames:
        dialogs = frame.locator('[role="dialog"], [role="alertdialog"], [aria-modal="true"], #onetrust-banner-sdk, #onetrust-consent-sdk, #usercentrics-root')
        for dialog in dialogs.all():
            if not dialog.is_visible() or not CONSENT_CONTEXT.search(dialog.inner_text()[:6000]):
                continue
            buttons = dialog.get_by_role('button', name=CONSENT_REJECT)
            visible = [button for button in buttons.all() if button.is_visible()]
            if len(visible) == 1:
                visible[0].click(timeout=3000)
                return True
    return False


class Browser:
    def __init__(self, profile_key):
        if not re.fullmatch(r'[a-f0-9]{64}', profile_key):
            raise ValueError('Invalid isolated browser profile')
        self.profile_key = profile_key
        self.context = self.page = self.session = self.root = None
        self.refs = {}
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()

    def close(self):
        if self.context:
            self.context.close()
        self.context = self.page = self.session = None
        self.refs = {}
        self.root = None  # Keep this account's dedicated profile, including consent choices.

    def start(self, sid):
        self.close()
        if not re.fullmatch(r'[a-f0-9]{32}', sid):
            raise ValueError('Invalid browser session')
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'.local/share'))) / 'OpenHermes' / 'browser-profiles'
        base.mkdir(parents=True, exist_ok=True)
        self.root = base / self.profile_key
        self.root.mkdir(exist_ok=True)
        if self.root.is_symlink() or self.root.resolve().parent != base.resolve():
            raise ValueError('Invalid isolated browser profile path')
        failure = None
        for channel in ('chrome', 'msedge'):
            try:
                self.context = self.playwright.chromium.launch_persistent_context(str(self.root), channel=channel,
                    headless=False, viewport={'width':1280,'height':900}, accept_downloads=False)
                break
            except Exception as exc:
                failure = exc
        if not self.context:
            raise RuntimeError('Install Google Chrome or Microsoft Edge before connecting') from failure
        self.session = sid
        # Defense in depth; validated on every request, including redirect targets.
        def route(request_route):
            request = request_route.request
            if request.url.startswith(('data:','blob:','about:')):
                return request_route.continue_()
            try:
                public_url(request.url)
            except Exception:
                return request_route.abort()
            request_route.continue_()
        self.context.route('**/*', route)
        self.context.set_default_timeout(15000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def request(self, body):
        action = body.get('action')
        if action == 'close':
            if self.session == body.get('session'):
                self.close()
            return {'success':True}
        if action not in ('navigate','refresh','back','scroll','click','type'):
            raise ValueError('Unsupported browser command')
        if self.session != body.get('session'):
            if action != 'navigate':
                raise ValueError('Open a website to start the local browser')
            self.start(body['session'])
        if self.page.is_closed():
            raise ValueError('Local browser window closed. Stop and start a new session.')
        if action == 'navigate':
            self.page.goto(public_url(body['url']), wait_until='domcontentloaded', timeout=45000)
        elif action in ('click','type'):
            locator = self.refs.get(body.get('ref'))
            if locator is None:
                raise ValueError('Element is not in the current local browser view')
            if action == 'click':
                locator.click()
            else:
                text = body.get('text')
                if not isinstance(text,str) or len(text)>4096:
                    raise ValueError('Invalid field text')
                locator.fill(text)
        elif action == 'back':
            self.page.go_back(wait_until='domcontentloaded')
        elif action == 'scroll':
            self.page.mouse.wheel(0, 650 if body.get('direction')=='down' else -650)
        # Refresh takes a new view, preserving a page the human has just navigated.
        self.page.wait_for_timeout(300)
        public_url(self.page.url)
        dismissed = reject_cookie_dialog(self.page)
        if dismissed:
            self.page.wait_for_timeout(200)
            public_url(self.page.url)
        result = self.frame()
        result['consent_dismissed'] = dismissed
        return result

    def frame(self):
        # Fixed DOM extraction: input values/contenteditable text never leave the PC.
        rows = self.page.locator('a,button,input,textarea,select,[role="button"],[role="link"],[contenteditable="true"]').evaluate_all('''els => els.map((e,i)=>({
          i, visible:!!(e.getClientRects().length), tag:e.tagName.toLowerCase(), type:e.type||'',
          role:e.getAttribute('role')||'', label:(e.getAttribute('aria-label')||e.getAttribute('placeholder')||
            (e.labels&&e.labels[0]?e.labels[0].textContent:'')||(['INPUT','TEXTAREA'].includes(e.tagName)||e.isContentEditable?'':e.innerText)||'').slice(0,180)
        }))''')
        all_elements = self.page.locator('a,button,input,textarea,select,[role="button"],[role="link"],[contenteditable="true"]')
        self.refs = {}
        lines = []
        for row in rows:
            if not row['visible'] or len(lines)>=350:
                continue
            ref = 'e'+str(len(lines)+1)
            self.refs[ref] = all_elements.nth(row['i'])
            role = row['role'] or ('link' if row['tag']=='a' else 'button' if row['tag']=='button' or row['type'] in ('submit','button') else 'searchbox' if row['type']=='search' else 'textbox' if row['tag'] in ('input','textarea') else row['tag'])
            label = re.sub(r'\s+', ' ', row['label']).strip()
            lines.append('- '+role+' '+json.dumps(label,ensure_ascii=False)+' [ref='+ref+']'+(' (password)' if row['type']=='password' else ''))
        # Product/page text, with user-entered content removed before reading.
        text = self.page.locator('body').evaluate('''body=>{const w=document.createTreeWalker(body,NodeFilter.SHOW_TEXT);let n,out='';while((n=w.nextNode())&&out.length<14000){const p=n.parentElement;if(p&&!p.closest('input,textarea,select,[contenteditable],script,style')&&p.getClientRects().length&&getComputedStyle(p).visibility!=='hidden')out+=n.textContent+' ';}return out.slice(0,14000)}''')
        snapshot = '\n'.join(lines)+'\nPage text:\n'+re.sub(r'\s+', ' ', text)
        png = self.page.screenshot(type='png', mask=[self.page.locator('input,textarea,select,[contenteditable]')], timeout=15000)
        meta = self.page.evaluate('''()=>({status:performance.getEntriesByType('navigation')[0]?.responseStatus||0,
            security:/kpsdk|ips\\.js/i.test(document.documentElement.outerHTML),text:document.body.innerText.trim().length})''')
        blocked = meta['security'] and meta['status']>=400 and meta['text']<80
        empty = not lines and not text.strip()
        return {'success':True,'url':self.page.url,'title':self.page.title(),'snapshot':snapshot[:80000],
                'image':'data:image/png;base64,'+base64.b64encode(png).decode() if len(png)<6*1024*1024 else None,
                'page_issue':'security_check' if blocked else 'empty_page' if empty else None,
                'warning':'The website returned HTTP '+str(meta['status'])+' with a security-check page. AI cannot read product results; review access in the local window.' if blocked else 'The website has no readable content in this browser. AI cannot verify results. Review the local window or use another website.' if empty else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    parser.add_argument('--gui', action='store_true', help='Show a pairing dialog without a terminal')
    parser.add_argument('--pair-code-file', help=argparse.SUPPRESS)
    args = parser.parse_args()
    server = args.server.rstrip('/')
    url = urlsplit(server)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.path:
        parser.error('Use your Open WebUI origin URL')
    if url.scheme == 'http':
        try:
            address = ipaddress.ip_address(url.hostname)
            trusted = address.is_private or address in ipaddress.ip_network('100.64.0.0/10')
        except ValueError:
            trusted = url.hostname in ('localhost',)
        if not trusted:
            parser.error('Public servers require HTTPS')
    def api(body):
        req = Request(server+'/api/openhermes/browser/local', data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
        with urlopen(req, timeout=100) as response:
            return json.load(response)
    if args.pair_code_file:
        code_path = Path(args.pair_code_file)
        code = code_path.read_text().strip()
        code_path.unlink()
    elif args.gui:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        code = simpledialog.askstring('OpenHermes local browser', 'Paste the pairing code from your Guided Browser page:', show='*')
        root.destroy()
        if not code:
            return
    else:
        code = getpass.getpass('Pairing code from your Guided Browser page (hidden): ')
    pairing = api({'action':'pair','code':code})
    token = pairing['token']
    code = None
    profile_key = hashlib.sha256((server+'|'+pairing['profile_id']).encode()).hexdigest()
    browser = Browser(profile_key)
    print('Connected. Use Guided Browser in Open WebUI. Pause AI before using the dedicated local window. Ctrl+C disconnects.')
    try:
        while True:
            task = api({'action':'poll','token':token})
            if not task.get('id'):
                time.sleep(1)
                continue
            try:
                result = browser.request(task['command'])
            except Exception:
                result = {'success':False,'error':'Local browser operation failed. Check the local window, then stop and restart the session if needed.'}
            try:
                api({'action':'result','token':token,'id':task['id'],'result':result})
            except HTTPError as exc:
                if exc.code != 409:
                    raise
                # A timed out command cannot keep a detached authenticated browser.
                browser.close()
    except KeyboardInterrupt:
        pass
    finally:
        browser.close()
        browser.playwright.stop()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        if '--gui' in __import__('sys').argv:
            import tkinter as tk
            from tkinter import messagebox
            root=tk.Tk();root.withdraw()
            messagebox.showerror('OpenHermes local browser', 'Connection ended. Check Open WebUI and pair again. If a browser session is active, stop it before pairing.')
            root.destroy()
        else:
            raise SystemExit('Connection ended. Check Open WebUI and pair again; no credentials were logged.')
