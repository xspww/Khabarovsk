export function renderAccountRows({rows,selectedUser,cfgFor,bucket,isCaptcha,rowStatusLabel,rowStatusTitle,avatarHtml,esc}){
  if(!rows.length){
    return '<tr><td colspan="5" style="height:100px;text-align:center;color:var(--muted)">No accounts match the current filter</td></tr>';
  }
  return rows.map((a,i)=>{
    const cfg=cfgFor(a.username),display=a.display||cfg.alias||a.username,b=bucket(a),desc=a.description??cfg.description??'',captcha=isCaptcha?.(a);
    const captchaActions=captcha?`<button class="small-btn captcha-resume" data-action="focus-captcha" data-user="${esc(a.username)}" title="Focus the Roblox CAPTCHA window">Focus</button><button class="small-btn captcha-resume" data-action="open-captcha-login" data-user="${esc(a.username)}" title="Open Roblox login to solve CAPTCHA manually">Login</button><button class="small-btn captcha-resume" data-action="resume-captcha" data-user="${esc(a.username)}" title="Resume after CAPTCHA is solved">Resume</button>`:'';
    return `<tr class="${a.username===selectedUser?'active':''}" data-user="${esc(a.username)}"><td class="num">${i+1}</td><td><span class="status ${b}" title="${esc(rowStatusTitle(a,b))}">${esc(rowStatusLabel(a,b))}</span></td><td><div class="user">${avatarHtml(a,display)}<div class="user-main"><div class="name">${esc(display)}</div><div class="handle">${esc(a.username)}</div></div></div></td><td><input class="desc-input" data-user="${esc(a.username)}" value="${esc(desc)}" placeholder="Add description"></td><td><div class="row-actions">${captchaActions}<button class="icon-btn danger" data-action="delete" data-user="${esc(a.username)}" title="Delete account" aria-label="Delete ${esc(a.username)}">&times;</button></div></td></tr>`;
  }).join('');
}
