import {richBody, toolFailed, toolTitle, toolDetails, taskStatus} from './messages.js';
'use strict';
const $ = id => document.getElementById(id);
let projects = [], current = null, currentFile = null, sha = null, saved = '', pathMode = 'file';
let chatId = null, chats = [], removedChat = null;
let jobs = [], polling = false, loadingJobs = false, displayedCommands = new Set();
const expanded = new Set();
const token = () => localStorage.getItem('token');

function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
async function api(action, data = {}) {
  const response = await fetch('/api/openhermes/projects/action', {method:'POST',
    headers:{'Content-Type':'application/json', Authorization:'Bearer ' + (token() || '')},
    body:JSON.stringify({action, ...(['run','jobs'].includes(action)&&chatId?{chat_id:chatId}:{}), ...(current ? {project_id:current.id} : {}), ...data})});
  const body = await response.json();
  if (response.status === 401) { location.href = '/auth?redirect=' + encodeURIComponent('/projects/'); throw new Error('Sign in to continue'); }
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : body.error || 'Request failed');
  return body;
}
function element(tag, className, text) { const el = document.createElement(tag); if (className) el.className=className; if (text !== undefined) el.textContent=text; return el; }
function setBusy(value) { $('runtime-state').textContent=value?'Running…':'Ready'; $('agent-status').textContent=value?'Working…':'Ready'; $('send').disabled=value; $('command-form').querySelector('button').disabled=value; $('stop-task').hidden=!value; }
const stopTask=element('button','stop-task','Stop');stopTask.id='stop-task';stopTask.hidden=true;$('agent-status').parentElement.append(stopTask);
stopTask.onclick=async()=>{try{stopTask.disabled=true;await api('stop');await loadJobs();}catch(err){notice(err.message);}finally{stopTask.disabled=false;}};
function dirty() { return currentFile !== null && $('editor').value !== saved; }
function updateDirty() { $('dirty').hidden=!dirty(); $('save').disabled=!dirty(); }
function lines() { window.ProjectEditor.sync(currentFile, editor.value); $('line-numbers').textContent=Array.from({length:$('editor').value.split('\n').length},(_,i)=>i+1).join('\n'); }
function createDialog() { $('create-dialog').showModal(); $('project-name').focus(); }
$('new-project').onclick=createDialog; $('welcome-create').onclick=createDialog;
$('cancel-create').onclick=()=>$('create-dialog').close();
$('create-form').onsubmit=async e=>{e.preventDefault();const button=$('create-submit');button.disabled=true;button.textContent='Preparing project…';try{const result=await api('create',{name:$('project-name').value,repository:$('repository').value.trim()||null});projects.unshift(result.project);renderProjects();$('create-dialog').close();$('create-form').reset();await selectProject(result.project);}catch(err){notice(err.message);}finally{button.disabled=false;button.textContent='Create project →';}};
function renderProjects() {
  $('projects-list').replaceChildren(); $('project-count').textContent=projects.length;
  for(const project of projects){const button=element('button','project-link'+(current?.id===project.id?' active':''));button.append(element('span','project-symbol','▧'),element('span','project-label',project.name));button.title=project.name;button.onclick=()=>selectProject(project);const row=element('div','project-row');const remove=element('button','project-delete','×');remove.title='Delete project';remove.setAttribute('aria-label','Delete project '+project.name);remove.onclick=async()=>{if(!confirm('Delete project “'+project.name+'” from your project list? Its conversations will be hidden. Files and Git history remain on the server for recovery.'))return;if(current?.id===project.id&&dirty()&&!confirm('Discard unsaved editor changes?'))return;try{remove.disabled=true;await api('delete_project',{project_id:project.id});projects=projects.filter(p=>p.id!==project.id);if(current?.id===project.id){current=null;currentFile=null;chatId=null;jobs=[];polling=false;history.replaceState(null,'',location.pathname);$('workspace').hidden=true;$('welcome').hidden=false;$('rename-project').hidden=true;setBusy(false);if(projects.length)await selectProject(projects[0]);}renderProjects();notice('Project deleted. Its files remain on the server for recovery.');}catch(e){notice(e.message);remove.disabled=false;}};row.append(button,remove);$('projects-list').append(row);}
}
async function selectProject(project) {
  if(dirty()&&!confirm('Discard your unsaved editor changes?'))return;
  current=project; removedChat=null;$('undo-remove').hidden=true;currentFile=null; sha=null; saved='';expanded.clear();displayedCommands.clear();jobs=[];
  history.replaceState(null,'','#'+project.id);$('welcome').hidden=true;$('workspace').hidden=false;
  $('project-title').textContent=project.name;$('root-label').textContent=project.slug;$('scope').textContent='⌘ '+project.name;
  $('rename-project').hidden=false;
  $('footer-state').textContent='main · '+project.name;$('file-name').textContent='Getting started';$('editor-empty').hidden=false;$('code-wrap').hidden=true;$('save').disabled=true;$('close-file').hidden=true;
  $('terminal-output').textContent="Your commands run inside this project's container.\n";$('preview').src='about:blank';
  renderProjects();notice('');await refreshTree();await loadChats();await loadJobs();
}
async function refreshTree() { if(!current)return; const container=$('file-tree');container.replaceChildren();await branch(container,'',0); }
async function branch(container,path,depth){const result=await api('tree',{path});for(const entry of result.entries){const row=element('button','file-row'+(entry.path===currentFile?' selected':''));row.style.paddingLeft=(12+depth*13)+'px';row.append(element('span','file-icon',entry.directory?(expanded.has(entry.path)?'▾':'▸'):'·'),document.createTextNode(entry.name));row.title=entry.path;row.setAttribute('role','treeitem');if(entry.directory)row.setAttribute('aria-expanded',expanded.has(entry.path));row.onclick=async()=>{try{if(entry.directory){if(expanded.has(entry.path))expanded.delete(entry.path);else expanded.add(entry.path);await refreshTree();}else await openFile(entry.path);}catch(err){notice(err.message);}};container.append(row);if(entry.directory&&expanded.has(entry.path))await branch(container,entry.path,depth+1);}}
async function openFile(path){if(dirty()&&!confirm('Discard your unsaved editor changes?'))return;const result=await api('read',{path});currentFile=path;$('close-file').hidden=false;sha=result.sha;saved=result.content;$('editor').value=saved;$('file-name').textContent=path;$('editor-empty').hidden=true;$('code-wrap').hidden=false;updateDirty();lines();await refreshTree();}
async function save(){if(!dirty())return;try{const result=await api('save',{path:currentFile,content:$('editor').value,sha});sha=result.sha;saved=$('editor').value;updateDirty();notice('');$('footer-detail').textContent='Saved '+currentFile;}catch(err){notice(err.message);}}
function closeFileNow(){currentFile=null;sha=null;saved='';$('editor').value='';window.ProjectEditor.sync(null,'');$('file-name').textContent='Getting started';$('dirty').hidden=true;$('save').disabled=true;$('close-file').hidden=true;$('editor-empty').hidden=false;$('code-wrap').hidden=true;$('close-file-dialog').close();refreshTree().catch(e=>notice(e.message));}
function requestCloseFile(){if(!currentFile)return;if(!dirty()){closeFileNow();return;}$('close-file-description').textContent='You have unsaved changes in '+currentFile+'.';$('close-file-dialog').showModal();$('cancel-close-file').focus();}
$('close-file').onclick=requestCloseFile;
$('cancel-close-file').onclick=()=>$('close-file-dialog').close();
$('discard-close-file').onclick=closeFileNow;
$('save-close-file').onclick=async()=>{await save();if(!dirty())closeFileNow();};
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='w'&&currentFile){e.preventDefault();requestCloseFile();}});
$('save').onclick=save;$('editor').oninput=()=>{lines();updateDirty();};$('editor').onscroll=()=>{$('line-numbers').scrollTop=$('editor').scrollTop;};
$('editor').onkeydown=e=>{if(e.key==='Tab'){e.preventDefault();const el=e.target,start=el.selectionStart,end=el.selectionEnd;el.setRangeText('  ',start,end,'end');lines();updateDirty();}};
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();save();}});
window.addEventListener('beforeunload',e=>{if(dirty()){e.preventDefault();e.returnValue='';}});
function pathDialog(mode){if(!current)return;pathMode=mode;$('path-dialog-title').textContent=mode==='file'?'New file':mode==='rename'?'Rename project':'New folder';document.querySelector('label[for="new-path"]').textContent=mode==='rename'?'Project name':'Project-relative path';$('new-path').value=mode==='rename'?current.name:'';$('new-path').placeholder=mode==='file'?'index.html':'src';$('path-dialog').showModal();$('new-path').focus();}
$('rename-project').onclick=()=>pathDialog('rename');
$('new-file').onclick=()=>pathDialog('file');$('new-folder').onclick=()=>pathDialog('folder');$('cancel-path').onclick=()=>$('path-dialog').close();
$('path-form').onsubmit=async e=>{e.preventDefault();const path=$('new-path').value;try{if(pathMode==='rename'){const result=await api('rename',{name:path});Object.assign(current,result.project);$('project-title').textContent=current.name;renderProjects();}else if(pathMode==='file')await api('save',{path,content:'',sha:null});else await api('mkdir',{path});$('path-dialog').close();await refreshTree();if(pathMode==='file')await openFile(path);}catch(err){notice(err.message);}};
document.querySelectorAll('[data-panel]').forEach(button=>button.onclick=()=>{for(const name of ['terminal','changes','preview'])$(name+'-panel').hidden=name!==button.dataset.panel;document.querySelectorAll('[data-panel]').forEach(b=>b.classList.toggle('active',b===button));if(button.dataset.panel==='changes')loadChanges();});
async function loadChanges(){try{const result=await api('changes');$('changes-output').textContent=(result.status||'Working tree clean.')+'\n'+result.diff;$('changes-count').textContent=result.status.trim()?result.status.trim().split('\n').length:'';}catch(err){notice(err.message);}}
$('load-changes').onclick=loadChanges;
$('load-preview').onclick=async()=>{try{const result=await api('preview_url');const path=$('preview-path').value||'index.html';$('preview').src=result.url+path.split('/').map(encodeURIComponent).join('/');}catch(err){notice(err.message);}};
function message(role,text){const block=element('article','message '+role);block.append(element('div','message-label',role==='user'?'You':'☤ Hermes'));if(text)block.append(element('div','message-body',text));return block;}
async function loadChats(){const result=await api('chats');chats=result.chats;chatId=localStorage.getItem('openhermes-chat:'+current.id);if(!chats.some(c=>c.id===chatId))chatId=chats[0]?.id;renderChats();}
function renderChats(){
 const chat=chats.find(c=>c.id===chatId);$('chat-title').textContent=chat?.title||'New chat';$('chat-title').title=chat?.title||'New chat';
 if(chatId)localStorage.setItem('openhermes-chat:'+current.id,chatId);else localStorage.removeItem('openhermes-chat:'+current.id);renderHistory();
}
function renderHistory(){
 const list=$('history-list');list.replaceChildren();const query=$('history-search').value.toLowerCase();
 const sorted=chats.slice().sort((a,b)=>(b.updated_at||b.created_at||0)-(a.updated_at||a.created_at||0));let group='';
 for(const chat of sorted.filter(c=>c.title.toLowerCase().includes(query))){
  const when=new Date((chat.updated_at||chat.created_at||Date.now()/1000)*1000),today=new Date(),yesterday=new Date();yesterday.setDate(today.getDate()-1);
  const heading=when.toDateString()===today.toDateString()?'Today':when.toDateString()===yesterday.toDateString()?'Yesterday':'Earlier';
  if(heading!==group){list.append(element('div','history-group',heading));group=heading;}
  const row=element('div','history-row'+(chat.id===chatId?' selected':''));
  const open=element('button','history-open');open.append(element('span','history-row-title',chat.title),element('small','',when.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})+(chat.id===chatId?' · Current':'')));
  open.onclick=async()=>{chatId=chat.id;renderChats();$('history-panel').hidden=true;jobs=[];polling=false;await loadJobs();$('prompt').focus();};
  const rename=element('button','history-rename','✎');rename.title='Rename conversation';rename.setAttribute('aria-label','Rename '+chat.title);rename.onclick=()=>{open.replaceChildren();const input=element('input');input.value=chat.title;input.setAttribute('aria-label','Conversation title');open.replaceWith(input);input.focus();input.select();let done=false;const finish=async()=>{if(done)return;done=true;const title=input.value.trim();if(title&&title!==chat.title){try{await api('rename_chat',{chat_id:chat.id,title});chat.title=title;}catch(e){notice(e.message);}}renderChats();};input.onkeydown=e=>{if(e.key==='Enter')finish();if(e.key==='Escape'){done=true;renderChats();}};input.onblur=finish;};
  const remove=element('button','history-remove','×');remove.title='Remove conversation';remove.setAttribute('aria-label','Remove '+chat.title);
  remove.onclick=async()=>{try{remove.disabled=true;await api('remove_chat',{chat_id:chat.id});removedChat={id:chat.id,project:current.id};chats=chats.filter(c=>c.id!==chat.id);if(chatId===chat.id){chatId=chats[0]?.id||null;jobs=[];polling=false;setBusy(false);if(chatId)await loadJobs();else renderMessages();}renderChats();$('undo-remove').hidden=false;}catch(e){notice(e.message);remove.disabled=false;}};
  row.append(open,rename,remove);list.append(row);
 }
 if(!list.children.length)list.append(element('p','history-empty','No conversations found.'));
}
$('show-history').onclick=async()=>{try{const result=await api('chats');chats=result.chats;renderHistory();$('history-panel').hidden=false;$('history-search').focus();}catch(e){notice(e.message);}};
$('undo-remove').onclick=async()=>{if(!removedChat||removedChat.project!==current.id)return;try{await api('restore_chat',{chat_id:removedChat.id});const result=await api('chats');chats=result.chats;chatId=removedChat.id;removedChat=null;renderChats();await loadJobs();$('undo-remove').hidden=true;}catch(e){notice(e.message);}};
$('close-history').onclick=()=>{$('history-panel').hidden=true;$('show-history').focus();};
$('history-search').oninput=renderHistory;
$('history-panel').onkeydown=e=>{if(e.key==='Escape')$('close-history').click();};
$('new-chat').onclick=async()=>{try{const result=await api('new_chat');chats.unshift(result.chat);chatId=result.chat.id;$('history-panel').hidden=true;renderChats();jobs=[];await loadJobs();$('prompt').focus();}catch(e){notice(e.message);}};
function renderMessages(){
 const list=$('messages'),oldScroll=list.scrollTop,atBottom=list.scrollHeight-list.scrollTop-list.clientHeight<70;
 const opened=new Set(Array.from(list.querySelectorAll('details[open][data-tool-key]'),el=>el.dataset.toolKey));list.replaceChildren();
 const agentJobs=jobs.filter(job=>job.kind==='agent').slice().reverse();
 if(!agentJobs.length){const intro=element('div','agent-intro');intro.append(element('h2','','New conversation'),element('p','','Ask Hermes to work on this project. Files are shared across chats; conversation context is separate.'));list.append(intro);return;}
 for(const job of agentJobs){list.append(message('user',job.prompt));const reply=message('assistant');let text='';
 const status=taskStatus(job);reply.append(element('div','task-status '+status.kind,status.label));
 for(const event of job.events){
  if(event.type==='tool_use'){
   const result=job.events.find(r=>r.type==='tool_result'&&r.tool_call_id&&r.tool_call_id===event.tool_call_id),failed=result&&toolFailed(result);
   const ended=!['running','queued'].includes(job.state);const state=failed?'Failed':result?'Succeeded':ended?'No result':'Running';
   const details=element('details','tool-event '+(failed?'tool-failed':result?'tool-done':'tool-running'));details.dataset.toolKey=job.id+':'+(event.tool_call_id||job.events.indexOf(event));details.open=opened.has(details.dataset.toolKey);
   const summary=element('summary');summary.append(element('span','tool-state',state),element('span','tool-name',toolTitle(event)));details.append(summary);
   details.append(toolDetails(event.input||{},'Arguments'));if(result)details.append(toolDetails(result.output,'Result'));reply.append(details);
  }
  if(event.type==='text')text+=event.text||'';if(event.type==='result')text=event.text||text;
 }
 if(text)reply.append(richBody(text));else if(['queued','running'].includes(job.state))reply.append(element('div','working-indicator','Working on your project…'));
 if(job.state==='failed'||job.state==='interrupted'){const detail=element('details','job-error');detail.append(element('summary','','Execution log'),element('pre','',job.output.slice(-1800)||'Task interrupted.'));reply.append(detail);}
 list.append(reply);}
 list.scrollTop=atBottom?list.scrollHeight:oldScroll;
}
async function loadJobs(){
  if(!current||loadingJobs)return;
  if(!chatId){jobs=[];setBusy(false);renderMessages();return;}
  loadingJobs=true;const pid=current.id,cid=chatId;
  try{
    const result=await api('jobs');if(pid!==current?.id||cid!==chatId)return;
    jobs=result.jobs;const running=jobs.some(j=>['queued','running'].includes(j.state));setBusy(running);renderMessages();
    const commandJobs=jobs.filter(j=>j.kind==='command').slice().reverse();
    $('terminal-output').textContent="Your commands run inside this project's container.\n"+commandJobs.map(job=>'\n❯ '+job.prompt+'\n'+job.output+(['queued','running'].includes(job.state)?'':'\n[exit '+job.exit_code+']\n')).join('');
    $('terminal-output').scrollTop=$('terminal-output').scrollHeight;
    if(polling&&!running){
      await refreshTree();
      if(currentFile&&!dirty()){
        const updated=await api('read',{path:currentFile}).catch(()=>null);
        if(updated){sha=updated.sha;saved=updated.content;$('editor').value=saved;lines();}
      }
      await loadChanges();
    }
    polling=running;
  }catch(err){notice(err.message);}finally{loadingJobs=false;}
}
async function run(kind,prompt){notice('');if(!chatId){const result=await api('new_chat');chats.unshift(result.chat);chatId=result.chat.id;renderChats();}if(kind==='agent'){const chat=chats.find(c=>c.id===chatId);if(chat&&['New chat','Original chat'].includes(chat.title)){chat.title=prompt.slice(0,65);renderChats();}}await api('run',{kind,prompt});setBusy(true);polling=true;await loadJobs();}
$('chat-form').onsubmit=async e=>{e.preventDefault();const prompt=$('prompt').value.trim();if(!prompt)return;try{await run('agent',prompt);$('prompt').value='';}catch(err){notice(err.message);setBusy(false);}};
$('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!$('send').disabled)$('chat-form').requestSubmit();}};
$('command-form').onsubmit=async e=>{e.preventDefault();const command=$('command').value.trim();if(!command)return;try{await run('command',command);$('command').value='';}catch(err){notice(err.message);}};
$('refresh').onclick=async()=>{try{await refreshTree();await loadJobs();}catch(err){notice(err.message);}};
async function boot(){try{const result=await api('list');projects=result.projects;$('user-name').textContent=result.user.name||'Your account';$('avatar').textContent=(result.user.name||'?')[0].toUpperCase();renderProjects();const requested=location.hash.slice(1);const project=projects.find(p=>p.id===requested)||projects[0];if(project)await selectProject(project);}catch(err){notice(err.message);}}
setInterval(()=>{if(current&&polling)loadJobs();},1300);
window.ProjectEditor.mount();
boot();
