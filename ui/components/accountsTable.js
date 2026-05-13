export function renderAccountRows({rows,selectedUser,cfgFor,bucket,isBlocked,rowStatusLabel,rowStatusTitle,avatarHtml,esc}){
  if(!rows.length){
    return '<tr><td colspan="5" style="height:100px;text-align:center;color:var(--muted)">No accounts match the current filter.</td></tr>';
  }
  return rows.map((a,i)=>{
    const cfg=cfgFor(a.username),display=a.display||cfg.alias||a.username,b=bucket(a),desc=a.description??cfg.description??'',blocked=isBlocked(a),reason=a.blocked_reason||'Cookie mismatch';
    return `<tr class="${a.username===selectedUser?'active':''}" data-user="${esc(a.username)}"><td class="num">${i+1}</td><td><span class="status ${b}" title="${esc(rowStatusTitle(a,b))}">${esc(rowStatusLabel(a,b))}</span></td><td><div class="user">${avatarHtml(a,display)}<div style="min-width:0"><div class="name">${esc(display)}</div><div class="handle">${esc(a.username)}${cfg.cookie_present?' - cookie':''}${a.cookie_username?' - '+esc(a.cookie_username):''}</div>${blocked?`<div class="blocked-note" title="${esc(reason)}">${esc(reason)}</div>`:''}</div></div></td><td><input class="desc-input" data-user="${esc(a.username)}" value="${esc(desc)}" placeholder="Add description"></td><td><div class="row-actions"><button class="icon-btn danger" data-action="delete" data-user="${esc(a.username)}" title="Delete account" aria-label="Delete ${esc(a.username)}">&times;</button></div></td></tr>`;
  }).join('');
}
