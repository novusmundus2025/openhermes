(() => {
  'use strict';
  const $=id=>document.getElementById(id), params=new URLSearchParams(location.search);
  const project_id=params.get('project_id'),path=params.get('path');
  let content='';
  $('project').href='/projects/#'+encodeURIComponent(project_id||'');
  $('name').textContent=path||'Project file';
  $('download').onclick=()=>{
    const url=URL.createObjectURL(new Blob([content],{type:'application/octet-stream'}));
    const link=document.createElement('a');link.href=url;link.download=(path||'file').split('/').pop();link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  (async()=>{
    try{
      const response=await fetch('/api/openhermes/projects/action',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+(localStorage.getItem('token')||'')},body:JSON.stringify({action:'read',project_id,path})});
      if(response.status===401){location.href='/auth?redirect='+encodeURIComponent(location.pathname+location.search);return;}
      const result=await response.json();
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'File could not be opened.');
      content=result.content;$('content').textContent=content;$('download').disabled=false;
      $('status').textContent='Saved on GX10 · current file contents. Download to save a copy on your computer.';
    }catch(error){$('status').textContent=error.message;}
  })();
})();
