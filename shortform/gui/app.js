const $ = id => document.getElementById(id);
const labels = {youtube:'YouTube Shorts', instagram:'Instagram Reels', tiktok:'TikTok'};
const icons = {youtube:'▶', instagram:'◎', tiktok:'♪'};
let videoId = null, videoName = "", available = [], planId = null, busy = false, checks = {};
function alertText(text) { $('alert').textContent = text; $('alert').hidden = !text; }
async function api(path, options={}) { const r = await fetch(path, options); const data = await r.json(); if (!r.ok) throw new Error(data.error || '요청에 실패했습니다'); return data; }
function post(path, data) { return api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}); }
function updateButton() { $('review').disabled = busy || !videoId || !document.querySelector('.platform input:checked'); }
async function loadAccounts() {
  try { const data = await api('/api/accounts'); available = data.available; const box = $('platforms'); box.replaceChildren();
    for (const p of Object.keys(labels)) { const row=document.createElement('div'); row.className='platform'; const icon=document.createElement('span'); icon.className='platform-icon'; icon.textContent=icons[p]; const label=document.createElement('label'); const check=document.createElement('input'); check.type='checkbox'; check.value=p; check.disabled=!available.includes(p) || !!checks[p]?.length; check.checked=!check.disabled; check.id='p-'+p; check.addEventListener('change',updateButton); label.htmlFor=check.id; label.textContent=labels[p]; const small=document.createElement('small'); small.textContent=data.unavailable[p]?.startsWith('미연결') ? '미연결' : (data.unavailable[p] || '연결됨'); label.append(small); const connect=document.createElement('button'); connect.className='connect'; connect.type='button'; connect.textContent=available.includes(p)?'다시 연결':'연결하기'; connect.addEventListener('click',()=>openSetup(p)); row.append(icon,label,connect,check); box.append(row); } updateButton();
  } catch(e) { alertText(e.message); }
}
async function selectFile(file) { if (!file) return; videoId=null; checks={}; updateButton(); alertText(''); if (!file.name.toLowerCase().endsWith('.mp4')) { alertText('MP4 파일을 선택해 주세요.'); return; } if(file.size > 4*1024**3) { alertText('영상은 4GB 이하여야 합니다.'); return; }
  busy=true; updateButton(); $('video-info').hidden=false; $('video-info').textContent='영상을 가져와 검사하고 있어요…';
  try { const data=await api('/api/videos',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file}); videoId=data.id; videoName=file.name.replace(/\.mp4$/i, ""); checks=data.checks.errors; const details=[file.name, `${(data.size/1024/1024).toFixed(1)} MB`, data.duration != null ? `${data.duration.toFixed(1)}초` : '', data.width && data.height ? `${data.width} × ${data.height}` : '', data.fps ? `${data.fps.toFixed(0)} fps` : ''].filter(Boolean); $('video-info').replaceChildren(); const info=document.createElement('strong'); info.textContent=details.join(' · '); $('video-info').append(info); for(const p of available) { const line=document.createElement('div'); line.textContent=checks[p]?.length ? `🚫 ${labels[p]}: ${checks[p].join('; ')}` : `✓ ${labels[p]}: 업로드 가능`; $('video-info').append(line); const checkbox=$('p-'+p); checkbox.disabled=!!checks[p]?.length; if(checkbox.disabled) checkbox.checked=false; } for(const warning of data.checks.warnings) { const line=document.createElement('div'); line.textContent='⚠ '+warning; $('video-info').append(line); }
  } catch(e) { alertText(e.message); $('video-info').hidden=true; } finally { busy=false; updateButton(); }
}
$('file').addEventListener('change', e=>selectFile(e.target.files[0])); const drop=$('drop'); for(const type of ['dragenter','dragover']) drop.addEventListener(type,e=>{e.preventDefault();drop.classList.add('drag');}); for(const type of ['dragleave','drop']) drop.addEventListener(type,e=>{e.preventDefault();drop.classList.remove('drag');}); drop.addEventListener('drop',e=>selectFile(e.dataTransfer.files[0]));
$('review').addEventListener('click',async()=>{ busy=true; updateButton(); alertText(''); try { const platforms=[...document.querySelectorAll('.platform input:checked')].map(e=>e.value); const data=await post('/api/plans',{video_id:videoId,caption:$('caption').value,hashtags:$('hashtags').value,title:$('title').value || (!$('caption').value.trim() ? videoName : ''),platforms}); planId=data.plan_id; $('summary').textContent=Object.entries(data.targets).map(([p,t])=>`${labels[p]} · ${t.account} · ${t.privacy}\n${Object.entries(t.metadata).map(([k,v])=>`${k}: ${v}`).join('\n')}`).join('\n\n') + (data.warnings.length ? '\n\n⚠ '+data.warnings.join('\n⚠ ') : ''); $('confirm').showModal(); } catch(e) {alertText(e.message)} finally {busy=false;updateButton()} });
$('submit').addEventListener('click',async()=>{ $('submit').disabled=true; try { const data=await post('/api/uploads',{plan_id:planId,confirmed:true}); $('confirm').close(); $('progress').hidden=false; busy=true; updateButton(); poll(data.id); } catch(e) { $('confirm').close(); alertText(e.message); } finally { $('submit').disabled=false; } });
async function poll(id) { try { const data=await api('/api/uploads/'+id); const box=$('progress-list'); box.replaceChildren(); for(const [p,rec] of Object.entries(data.platforms)){const row=document.createElement('div');row.className='status';const text=document.createElement('div'); const states={queued:'대기 중',publishing:'업로드 중',published:rec.delivery==='inbox'?'받은편지함 전송 완료':'완료',failed:'실패',unknown:'확인 필요'}; text.textContent=`${icons[p]} ${labels[p]} · ${states[rec.state]||rec.state}`; row.append(text); if(rec.error){const detail=document.createElement('div');detail.textContent=rec.error;row.append(detail)} if(rec.url && /^https:\/\//.test(rec.url)){const link=document.createElement('a');link.href=rec.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=rec.url;row.append(link)} if(rec.note){const note=document.createElement('div');note.textContent=rec.note;row.append(note)} box.append(row); } if(data.error) alertText(data.error); if(!data.done) setTimeout(()=>poll(id),1000); else {busy=false;updateButton()} } catch(e) {alertText(e.message); busy=false;updateButton()} }
let setupPlatform = null, setupSteps = [], setupIndex = 0, authTimer = null;
const element = (tag, value) => { const el=document.createElement(tag); if(value) el.textContent=value; return el; };
async function openSetup(platform) {
  setupPlatform=platform; $('setup-error').hidden=true; $('setup-heading').textContent=labels[platform]+' 연결';
  try { const data=await api(`/api/setup/${platform}/steps`); setupSteps=data.steps; setupIndex=setupSteps.findIndex(s=>!s.done); if(setupIndex<0) setupIndex=setupSteps.length; renderSetup(); $('setup-dialog').showModal(); }
  catch(e){alertText(e.message)}
}
function setupError(message) { $('setup-error').textContent=message; $('setup-error').hidden=!message; }
function renderSetup() {
  setupError(''); const box=$('setup-content'); box.replaceChildren(); $('setup-back').disabled=setupIndex===0; $('setup-next').disabled=false;
  if(setupIndex>=setupSteps.length) { $('setup-count').textContent='마지막 단계'; box.append(element('h3','계정 로그인 및 권한 허용'),element('p','로그인 화면을 열고 업로드할 계정으로 로그인한 뒤 권한을 허용해 주세요.')); $('setup-next').textContent='로그인 시작'; return; }
  const step=setupSteps[setupIndex]; $('setup-count').textContent=`${setupIndex+1} / ${setupSteps.length}`; box.append(element('h3',step.title),element('p',step.why)); const list=element('ol'); for(const action of step.actions) list.append(element('li',action)); box.append(list);
  if(step.success) box.append(element('p','✓ '+step.success));
  if(step.url) {const link=element('a','설정 페이지 열기 ↗');link.href=step.url;link.target='_blank';link.rel='noopener noreferrer';box.append(link)}
  if(step.copy) {const label=element('p','복사할 값');const value=element('code',step.copy);value.className='copy-value';label.append(value);box.append(label)}
  if(step.prompt) {const label=element('label',step.prompt);label.className='field';const input=element('input');input.id='setup-value';input.type=step.secret?'password':'text';input.autocomplete='off';label.append(input);box.append(label)}
  if(step.help.length) {const details=element('details');details.append(element('summary','도움말'));for(const h of step.help) details.append(element('p',h));box.append(details)}
  $('setup-next').textContent='다음';
}
$('setup-close').addEventListener('click',()=>{clearTimeout(authTimer); $('setup-dialog').close();loadAccounts()});
$('setup-back').addEventListener('click',()=>{if(setupIndex>0){setupIndex--;renderSetup()}});
$('setup-next').addEventListener('click',async()=>{const button=$('setup-next');button.disabled=true;setupError('');try {
 if(setupIndex<setupSteps.length){const step=setupSteps[setupIndex];await post(`/api/setup/${setupPlatform}/step`,{step:step.id,value:$('setup-value')?.value||''});setupIndex++;renderSetup()}
 else {await post(`/api/setup/${setupPlatform}/start`,{});showAuth();}
 }catch(e){setupError(e.message);button.disabled=false}});
async function showAuth(){
 try {const data=await api(`/api/setup/${setupPlatform}/auth`);const box=$('setup-content');
 if(data.state==='starting'){authTimer=setTimeout(showAuth,500);return}
 if(data.state==='done'){box.replaceChildren(element('h3','연결 완료'),element('p','계정이 연결되었습니다. 창을 닫고 업로드를 계속하세요.'));$('setup-next').hidden=true;$('setup-back').hidden=true;loadAccounts();return}
 if(data.state==='error'){setupError(data.error);$('setup-next').disabled=false;$('setup-next').textContent='다시 시도';return}
 if(data.url && !box.querySelector('.auth-link')) {box.replaceChildren(element('h3','로그인 및 권한 허용'),element('p','아래 버튼으로 로그인 화면을 열어 주세요.'));const link=element('a','로그인 화면 열기 ↗');link.href=data.url;link.target='_blank';link.rel='noopener noreferrer';link.className='auth-link';box.append(link);
 if(data.state==='callback'){box.append(element('p','승인 후 바뀐 브라우저 주소 전체를 아래에 붙여 넣어 주세요.'));const input=element('input');input.id='callback-url';input.placeholder='https://…';input.autocomplete='off';box.append(input);const submit=element('button','주소 확인');submit.type='button';submit.className='secondary';submit.addEventListener('click',async()=>{submit.disabled=true;try{await post(`/api/setup/${setupPlatform}/callback`,{url:input.value});input.value='';submit.textContent='확인 중…'}catch(e){setupError(e.message);submit.disabled=false}});box.append(submit)} else box.append(element('p','로그인을 마치면 자동으로 연결됩니다.'));
 }
 authTimer=setTimeout(showAuth,1000);
 }catch(e){setupError(e.message);$('setup-next').disabled=false}
}
$('setup-dialog').addEventListener('close',()=>{clearTimeout(authTimer);$('setup-next').hidden=false;$('setup-back').hidden=false});
loadAccounts();
