export function apiToken(){
  return document.querySelector('meta[name="cronus-api-token"]')?.content||'';
}

export async function api(path,method='GET',body){
  const opt={method,headers:{}};
  const token=apiToken();
  if(token)opt.headers['X-Cronus-Token']=token;
  if(body!==undefined){
    opt.headers['Content-Type']='application/json';
    opt.body=JSON.stringify(body);
  }
  const r=await fetch('/api'+path,opt);
  let data={};
  try{data=await r.json()}catch(e){}
  if(!r.ok)throw new Error(data.detail||data.msg||r.statusText);
  return data;
}
