import {solarIcon} from './icons.js';

export function createFeedback({$,esc}){
  function toast(msg){
    const el=$('toast');
    el.innerHTML=`<span class="toast-icon" aria-hidden="true">${solarIcon('noticeBell')}</span><span class="toast-text">${esc(msg)}</span><button class="toast-close" aria-label="Close notification">&times;</button>`;
    el.classList.add('show');
    const close=el.querySelector('.toast-close');
    if(close)close.onclick=()=>{el.classList.remove('show');clearTimeout(toast.t)};
    clearTimeout(toast.t);
    toast.t=setTimeout(()=>el.classList.remove('show'),3200);
  }
  function modal(title,body,foot){$('modal-title').textContent=title;$('modal-body').innerHTML=body;$('modal-foot').innerHTML=foot;$('modal-backdrop').hidden=false}
  function closeModal(){$('modal-backdrop').hidden=true}
  function cardIcon(type){const icons={cookie:'userAdd',delete:'trash',close:'trash',install:'downloadSquare'};return`<span class="choice-icon">${solarIcon(icons[type]||icons.cookie)}</span>`}
  return {toast,modal,closeModal,cardIcon};
}
