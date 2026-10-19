(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let sid = null, view = 0, ready = false, busy = false, control='human', polling=false;
  function controls() {
    for (const id of ['back','refresh','up','down','click','type']) $(id).disabled = busy || !ready || control!=='human';
    $('start').disabled = busy || !!sid; $('stop').disabled = busy || !sid;
    $('navigate').querySelector('button').disabled = busy || control!=='human';
    $('auto-start').disabled=busy||!ready||control!=='human';
    $('pause').disabled=busy||!sid||control==='human';
    $('text').disabled=control!=='human';
    $('control').textContent=control==='auto'?'AI control':control==='pausing'?'Finishing action before handoff…':'Human control · AI paused';
  }
  async function action(action, data = {}) {
    if (busy) return;
    busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Working…';
    try {
      const response = await fetch('/api/openhermes/browser/action', {method:'POST', headers:{'Content-Type':'application/json',Authorization:'Bearer '+(localStorage.getItem('token')||'')}, body:JSON.stringify({action,session_id:sid,view,...data})});
      if (response.status === 401) { location.href='/auth?redirect='+encodeURIComponent('/guided-browser/'); return; }
      const result = await response.json();
      if (!response.ok) {if(response.status>=500||response.status===404){sid=null;ready=false;$('screenshot').hidden=true;$('screenshot').removeAttribute('src');}throw new Error(typeof result.detail==='string'?result.detail:result.error||'Browser action failed');}
      sid=result.session_id||null; view=result.view||0;
      control=result.control||'human';
      if(action==='status')ready=!!sid&&view>0;
      if (!result.active) {ready=false;$('screenshot').hidden=true;$('screenshot').removeAttribute('src');$('snapshot').textContent='';$('empty').hidden=false;$('page-title').textContent='Open a website to begin';}
      if (result.snapshot !== undefined) {ready=true;$('snapshot').textContent=result.snapshot;$('url').value=result.url;$('page-title').textContent=result.title||result.url;$('empty').hidden=!!result.image;$('screenshot').hidden=!result.image;if(result.image)$('screenshot').src=result.image;else $('screenshot').removeAttribute('src');}
      $('status').textContent=sid?'Ready · view '+view:'No browser session';
      if(result.auto){$('auto-summary').textContent=result.auto.summary+' ('+result.auto.steps+'/'+result.auto.max_steps+' steps)';$('handoff').hidden=result.auto.state!=='human_required';}
      if(control==='auto'||control==='pausing')schedulePoll();
      if(result.warning){$('error').textContent=result.warning;$('error').hidden=false;}
      return result;
    } catch(error) {$('error').textContent=error.message;$('error').hidden=false;$('status').textContent='Action failed';}
    finally {busy=false;controls();}
  }
  function schedulePoll(){if(polling)return;polling=true;setTimeout(async()=>{polling=false;if(!busy&&sid)await action('auto_status');else if(sid)schedulePoll();},1500);}
  $('start').onclick=()=>action('start');
  $('stop').onclick=()=>action('stop');
  $('navigate').onsubmit=async event=>{event.preventDefault();if(!sid)await action('start');if(sid)await action('navigate',{url:$('url').value});};
  $('refresh').onclick=()=>action('refresh');$('back').onclick=()=>action('back');
  $('up').onclick=()=>action('scroll',{direction:'up'});$('down').onclick=()=>action('scroll',{direction:'down'});
  $('click-form').onsubmit=event=>{event.preventDefault();action('click',{ref:$('ref').value});};
  $('type-form').onsubmit=event=>{event.preventDefault();const text=$('text').value;$('text').value='';action('type',{ref:$('ref').value,text});};
  $('pause').onclick=()=>action('pause');
  $('auto-start').onclick=async()=>{const refreshed=await action('refresh');if(refreshed)await action('auto_start',{goal:$('goal').value.trim(),max_steps:Number($('steps').value)});};
  controls();action('status');
})();
