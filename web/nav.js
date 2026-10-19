(() => {
  const rowId = 'openhermes-projects-row';
  function install() {
    const anchor = document.getElementById('sidebar-notes-button');
    if (!anchor) return;
    const notesRow = anchor.closest('[data-id="notes"]') || anchor.parentElement;
    const existing = document.getElementById(rowId);
    if (existing && existing.parentElement === notesRow.parentElement) return;
    if (existing) existing.remove();
    document.getElementById('openhermes-projects-link')?.remove();
    const row = notesRow.cloneNode(false);
    row.id = rowId;
    row.dataset.id = 'openhermes-projects';
    const link = anchor.cloneNode(true);
    link.id = 'openhermes-projects-link';
    link.href = '/projects/';
    link.setAttribute('aria-label', 'Projects');
    link.removeAttribute('style');
    link.removeAttribute('aria-current');
    link.className = link.className.split(/\s+/).filter(c => !c.includes('bg-') || c.includes('hover:')).join(' ');
    const walker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (walker.currentNode.textContent.trim() === 'Notes') walker.currentNode.textContent = 'Projects';
    }
    const icon = link.querySelector('svg');
    if (icon) icon.innerHTML = '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3V7Z" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"/><path d="m9 11-2 2 2 2m6-4 2 2-2 2" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"/>';
    row.append(link);
    notesRow.insertAdjacentElement('afterend', row);
  }
  let queued = false;
  function schedule() {
    if (queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; install(); });
  }
  new MutationObserver(schedule).observe(document.documentElement, {childList:true,subtree:true});
  window.addEventListener('pageshow', schedule);
  window.addEventListener('popstate', schedule);
  install();
})();
