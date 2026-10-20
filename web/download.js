(() => {
  'use strict';
  const button=document.getElementById('download-project');
  if(!button)return;
  const standalone=document.getElementById('download-status');
  const status=standalone || document.getElementById('notice');
  function projectId(){return standalone?new URLSearchParams(location.search).get('project_id'):location.hash.slice(1);}
  function update(){button.hidden=!/^[a-f0-9]{32}$/.test(projectId()||'');}
  update();window.addEventListener('hashchange',update);
  const title=document.getElementById('project-title');
  if(title)new MutationObserver(update).observe(title,{childList:true,subtree:true});
  button.addEventListener('click',async()=>{
    button.disabled=true;const old=button.textContent;button.textContent='Preparing ZIP…';
    try{
      const response=await fetch('/api/openhermes/projects/'+encodeURIComponent(projectId())+'/download',{
        headers:{Authorization:'Bearer '+(localStorage.getItem('token')||'')}});
      if(response.status===401){location.href='/auth?redirect='+encodeURIComponent(location.pathname+location.search);return;}
      if(!response.ok){const error=await response.json();throw new Error(typeof error.detail==='string'?error.detail:'Download could not be prepared.');}
      const blob=await response.blob();const url=URL.createObjectURL(blob);
      const link=document.createElement('a');link.href=url;
      link.download=(response.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/)?.[1]||'project.zip';
      document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
      if(status){status.hidden=false;status.textContent='Download started: '+link.download+'. Includes saved source files; ignored files, Git history and known credential files are excluded.';}
    }catch(error){if(status){status.hidden=false;status.textContent=error.message;}}
    finally{button.disabled=false;button.textContent=old;}
  });
})();
