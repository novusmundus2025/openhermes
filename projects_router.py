"""Open WebUI extension: use its verified session on every Projects request."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import time
import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, JSONResponse
from open_webui.utils.auth import get_verified_user
from open_webui.models.users import Users

router = APIRouter()
ROOT = Path(__file__).resolve().parent


def config():
    return json.loads(Path('/app/backend/data/openhermes-bridge.json').read_text())


async def forward(user, body, endpoint='projects'):
    settings = config()
    identity = {k: getattr(user, k, '') for k in ('id', 'name', 'email')}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as session:
        async with session.post(settings['url'] + '/' + endpoint,
                headers={'Authorization': 'Bearer ' + settings['key']},
                json={'user': identity, 'body': body}) as response:
            data = await response.json()
            if response.status != 200:
                raise HTTPException(response.status, data.get('error', 'Project request failed'))
            return data


@router.post('/api/openhermes/browser/action')
async def browser_action(request: Request, user=Depends(get_verified_user)):
    if user.role not in ('admin', 'user'):
        raise HTTPException(403, 'Account approval is required')
    if int(request.headers.get('content-length', '0')) > 16384:
        raise HTTPException(413, 'Browser request too large')
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, 'Invalid browser request')
    return JSONResponse(await forward(user, body, 'browser'), headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})


@router.get('/guided-browser/')
async def guided_page():
    return FileResponse(ROOT / 'web' / 'guided.html', headers={'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"})


@router.post('/api/openhermes/browser/local')
async def local_device(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 9*1024*1024:
            raise HTTPException(413, 'Device response too large')
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get('action') not in ('pair','poll','result'):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, 'Invalid device request')
    settings = config()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100)) as session:
        async with session.post(settings['url']+'/browser-local', headers={'Authorization':'Bearer '+settings['key']}, json={'body':body}) as response:
            data = await response.json()
            return JSONResponse(data, status_code=response.status, headers={'Cache-Control':'no-store'})


@router.get('/guided-browser/local-companion.py')
async def local_companion():
    return FileResponse(ROOT / 'local_browser_companion.py', filename='local_browser_companion.py', headers={'Cache-Control':'no-store'})


@router.post('/api/openhermes/projects/action')
async def action(request: Request, user=Depends(get_verified_user)):
    if user.role not in ('admin', 'user'):
        raise HTTPException(403, 'Account approval is required')
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, 'Invalid project request')
    if body.get('action') == 'preview_url':
        # First check ownership, then issue an expiring read-only ticket for iframe assets.
        await forward(user, {'action': 'tree', 'project_id': body.get('project_id')})
        payload = base64.urlsafe_b64encode(json.dumps({'uid': user.id,
            'pid': body['project_id'], 'exp': int(time.time()) + 600}, separators=(',', ':')).encode()).decode().rstrip('=')
        mac = hmac.new(config()['key'].encode(), ('preview:' + payload).encode(), hashlib.sha256).hexdigest()
        return {'url': '/api/openhermes/projects/preview/' + payload + '.' + mac + '/'}
    return await forward(user, body)


@router.get('/api/openhermes/projects/preview/{ticket}/{path:path}')
async def preview(ticket: str, path: str):
    try:
        payload, signature = ticket.split('.')
        expected = hmac.new(config()['key'].encode(), ('preview:' + payload).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        values = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if values['exp'] < time.time():
            raise ValueError()
        user = await Users.get_user_by_id(values['uid'])
        if user is None or user.role not in ('user', 'admin'):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise HTTPException(403, 'Preview expired. Refresh the preview.')
    data = await forward(user, {'action': 'preview', 'project_id': values['pid'], 'path': path or 'index.html'})
    return Response(base64.b64decode(data['data']), media_type=data['mime'], headers={
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "sandbox allow-scripts; default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'",
        'Access-Control-Allow-Origin': '*', 'Referrer-Policy': 'no-referrer'})


@router.get('/projects')
async def projects_redirect():
    return RedirectResponse('/projects/')


@router.get('/projects/')
async def projects_page():
    return FileResponse(ROOT / 'web' / 'index.html', headers={'Cache-Control': 'no-store'})


@router.get('/project-file/')
async def project_file():
    return FileResponse(ROOT / 'web' / 'file.html', headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'"})


@router.get('/projects/{asset}')
async def project_asset(asset: str):
    if asset not in ('app.js', 'style.css', 'nav.js', 'guided.js', 'guided.css', 'file.js', 'download.js'):
        raise HTTPException(404)
    return FileResponse(ROOT / 'web' / asset, headers={'Cache-Control': 'no-cache'})


@router.get('/api/openhermes/projects/{project_id}/download')
async def project_download(project_id: str, user=Depends(get_verified_user)):
    if user.role not in ('admin','user'):raise HTTPException(403,'Account approval is required')
    data=await forward(user,{'action':'export','project_id':project_id})
    return Response(base64.b64decode(data['data']),media_type='application/zip',headers={
        'Content-Disposition':'attachment; filename="'+data['filename']+'"',
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.get('/project-download/')
async def download_page():
    return FileResponse(ROOT/'web'/'download.html',headers={'Cache-Control':'no-store',
        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'"})
