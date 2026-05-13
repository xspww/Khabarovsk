export function createFeedback({$,esc}){
  function toast(msg){
    const el=$('toast');
    el.innerHTML=`<span class="toast-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg></span><span class="toast-text">${esc(msg)}</span><button class="toast-close" aria-label="Close notification">&times;</button>`;
    el.classList.add('show');
    const close=el.querySelector('.toast-close');
    if(close)close.onclick=()=>{el.classList.remove('show');clearTimeout(toast.t)};
    clearTimeout(toast.t);
    toast.t=setTimeout(()=>el.classList.remove('show'),3200);
  }
  function modal(title,body,foot){$('modal-title').textContent=title;$('modal-body').innerHTML=body;$('modal-foot').innerHTML=foot;$('modal-backdrop').hidden=false}
  function closeModal(){$('modal-backdrop').hidden=true}
  function cardIcon(type){const icons={cookie:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="8"/><circle cx="9" cy="9" r="1"/><circle cx="14.5" cy="10.5" r="1"/><circle cx="11" cy="15" r="1"/><path d="M15.5 16.5h.01"/></svg>',delete:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 15H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>',close:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M9 9l6 6"/><path d="M15 9l-6 6"/></svg>',install:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>',log:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 4h14v16H5z"/><path d="M8 8h8"/><path d="M8 12h8"/><path d="M8 16h5"/></svg>'};return`<span class="choice-icon">${icons[type]||icons.cookie}</span>`}
  return {toast,modal,closeModal,cardIcon};
}
