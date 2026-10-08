// Mock security token (CV14 A). Shows the pending high-amount approval and its 6-digit code.
// Read-only: it never approves anything; the code must be typed into DCTA by the user.
'use strict';
const $=id=>document.getElementById(id);
const token=document.querySelector('meta[name="dcta-token"]').content;
try{const t=localStorage.getItem('dcta.theme');if(['light','grey','dark'].includes(t))document.documentElement.dataset.theme=t;}catch{}
let current=null,skew=0,shownKey='';
function show(data){
  current=data.active?data:null;
  $('tokenIdle').hidden=!!current;$('tokenActive').hidden=!current;
  if(!current){shownKey='';return;}
  skew=data.now_ms-Date.now();
  const key=`${data.code}|${data.expires_ms}`;
  if(key!==shownKey){
    shownKey=key;
    $('tokenTotal').textContent=data.total;
    $('tokenLines').replaceChildren(...data.lines.map(text=>{const li=document.createElement('li');li.textContent=text;return li;}));
    $('tokenCode').textContent=`${data.code.slice(0,3)} ${data.code.slice(3)}`;
    $('tokenCode').setAttribute('aria-label',`Code ${data.code.split('').join(' ')}`);
  }
  tick();
}
function tick(){
  if(!current)return;
  const left=Math.max(0,current.expires_ms-(Date.now()+skew));
  $('tokenExpiry').textContent=left>0?`Expires in ${Math.ceil(left/1000)} s`:'Expired. Ask DCTA for a new code.';
  $('tokenBar').style.width=`${Math.min(100,left/1200)}%`;
}
async function poll(){
  try{
    const response=await fetch('/api/token',{headers:{'X-DCTA-Token':token},cache:'no-store'});
    const data=await response.json();
    if(!response.ok)throw Error(data.message||'Token unavailable.');
    $('tokenError').hidden=true;show(data);
  }catch(e){$('tokenError').textContent=`${e.message} Keep this window open; it retries automatically.`;$('tokenError').hidden=false;}
}
poll();setInterval(poll,1500);setInterval(tick,500);
