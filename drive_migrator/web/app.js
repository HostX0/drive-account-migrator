'use strict';
const token = location.hash.slice(1) || sessionStorage.getItem('local-session') || '';
if (token) sessionStorage.setItem('local-session', token);
history.replaceState(null, '', '/');
const $ = id => document.getElementById(id);
let current = null, reviewed = null, loaded = false;
const configKeys = ['source_email','destination_email','source_root_id','destination_root_id','controller_id','state_dir'];
async function api(path, body) {
  const response = await fetch('/api/'+path, {method: body === undefined ? 'GET' : 'POST',
    headers: {'X-Session-Token': token, ...(body === undefined ? {} : {'Content-Type':'application/json'})},
    ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Request failed');
  return value;
}
function notice(message) { $('notice').textContent=message; $('notice').hidden=!message; $('review-error').textContent=message; $('review-error').hidden=!message; }
function tab(name) {
  document.querySelectorAll('.page').forEach(el=>el.classList.toggle('active', el.id===name));
  document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active', el.dataset.tab===name));
  window.scrollTo({top:0,behavior:'smooth'});
}
function fill(config) {
  configKeys.forEach(key=>$(key).value=config[key] || '');
  $('share_source').checked=config.share_source===true;
}
function config() {
  const result={...(current?.config || {}), blocked_source_ids:current?.config?.blocked_source_ids || []};
  configKeys.forEach(key=>result[key]=$(key).value.trim());
  result.share_source=$('share_source').checked;
  return result;
}
async function save() { const result=await api('config', config()); await refresh(); return result; }
async function run(action, extra={}) {
  notice(''); await save();
  await api('run', {action, max_items:Number($('max-items').value), ...extra});
  await refresh();
}
async function refresh() {
  const before=current;
  current=await api('state');
  if (!loaded || (before?.busy && !current.busy && before.action==='init-controller')) {fill(current.config);loaded=true;}
  $('client-state').textContent=current.has_client?'Connection file saved privately ✓':'No file selected yet';
  for (const role of ['source','destination']) {
    const connected=current.connections[role];
    $(role+'-state').textContent=connected?'CONNECTED':'NOT CONNECTED';
    $(role+'-state').classList.toggle('connected',connected);
  }
  $('run-status').textContent=current.busy?'Running '+current.action+' — keep the terminal open':
    current.last_exit===0?'Run finished. Review the result before continuing.':
    current.last_exit===null?'Ready when you are':'Stopped. Check the activity log; no automatic retry.';
  document.body.classList.toggle('running',current.busy);
  $('stop').disabled=!current.busy;
  document.querySelectorAll('main button, main input').forEach(el=>{
    if (el.id==='stop'||el.id==='close-dialog'||el.classList.contains('quiet')) return;
    el.disabled=current.busy;
  });
  $('log').textContent=current.log.join('\n') || 'No operations started.';
  $('auth-link').hidden=!current.auth_url;
  if (current.auth_url) $('auth-link').href=current.auth_url;
}
function safe(action) { return async event=>{if(event)event.preventDefault();try {await action(event);}catch(error){notice(error.message);}}; }
function cell(row, text, tag='td') {const element=document.createElement(tag);element.textContent=String(text ?? '');row.append(element);}
async function showReport(name, cleanupPhase=null) {
  notice(''); const result=await api('report/'+name), report=result.report;
  reviewed=cleanupPhase?{phase:cleanupPhase, hash:result.sha256}:null;
  $('review-title').textContent=cleanupPhase?(cleanupPhase==='purge'?'Review permanent deletion':'Review move to Trash'):'Migration report';
  $('confirm-area').hidden=!cleanupPhase;
  $('report-content').replaceChildren();
  if (cleanupPhase) {
    $('review-summary').textContent=report.items.length+' eligible files • '+report.excluded.length+' excluded • source: '+report.source_email;
    const table=document.createElement('table'), head=document.createElement('tr');
    for (const title of ['Name','Bytes','Source ID','Destination ID']) cell(head,title,'th');
    table.append(head);
    for (const item of report.items) {const row=document.createElement('tr');cell(row,item.source.name);cell(row,item.source.size);cell(row,item.source.id);cell(row,item.destination.id);table.append(row);}
    $('report-content').append(table);
    const phrase=cleanupPhase==='trash'?'TRASH_VERIFIED_ORIGINALS':'PERMANENTLY_DELETE_VERIFIED_ORIGINALS';
    $('phrase').textContent=phrase;$('acknowledge').value='';
    $('delete-warning').textContent=cleanupPhase==='trash'?'This moves only the reviewed originals to Trash. Trash still uses storage.':'This permanently deletes the reviewed originals. It cannot be undone. Check your copies before continuing.';
    $('confirm-action').disabled=report.items.length===0;
  } else {
    $('review-summary').textContent='Private local report. Do not post file IDs or personal information publicly.';
    const pre=document.createElement('pre');pre.textContent=JSON.stringify(report,null,2);$('report-content').append(pre);
  }
  $('review-dialog').showModal();
}
document.querySelectorAll('.nav').forEach(el=>el.addEventListener('click',()=>tab(el.dataset.tab)));
$('help-button').addEventListener('click',()=>tab('help'));
$('configuration').addEventListener('submit',safe(async()=>{await save();notice('Setup saved locally.');}));
$('client-file').addEventListener('change',safe(async event=>{
  const file=event.target.files[0];if(!file)return;
  if(file.size>262144)throw new Error('Choose the small OAuth client JSON file.');
  await api('client',JSON.parse(await file.text()));event.target.value='';await refresh();
}));
$('config-file').addEventListener('change',safe(async event=>{
  const file=event.target.files[0];if(!file)return;
  if(file.size>262144)throw new Error('Configuration file is too large.');
  const value=JSON.parse(await file.text());await api('config',value);fill(value);await refresh();event.target.value='';
}));
$('connect-source').addEventListener('click',safe(()=>run('connect',{role:'source'})));
$('connect-destination').addEventListener('click',safe(()=>run('connect',{role:'destination'})));
$('connect-cleanup').addEventListener('click',safe(()=>run('connect',{role:'source-cleanup'})));
$('init-controller').addEventListener('click',safe(()=>run('init-controller')));
$('preview-transfer').addEventListener('click',safe(()=>run('plan')));
$('start-copy').addEventListener('click',safe(()=>run('copy')));
$('check-storage').addEventListener('click',safe(()=>run('status')));
$('stop').addEventListener('click',safe(async()=>{await api('stop',{});notice('Stop requested. Waiting for a safe boundary and checkpoint.');}));
document.querySelectorAll('[data-plan]').forEach(el=>el.addEventListener('click',safe(()=>run('cleanup-plan',{phase:el.dataset.plan}))));
document.querySelectorAll('[data-review]').forEach(el=>el.addEventListener('click',safe(()=>showReport(el.dataset.review+'-plan',el.dataset.review))));
document.querySelectorAll('[data-report]').forEach(el=>el.addEventListener('click',safe(()=>showReport(el.dataset.report))));
$('close-dialog').addEventListener('click',()=>$('review-dialog').close());
$('confirm-action').addEventListener('click',safe(async()=>{
  if(!reviewed)throw new Error('Review a plan first.');
  const phrase=reviewed.phase==='trash'?'TRASH_VERIFIED_ORIGINALS':'PERMANENTLY_DELETE_VERIFIED_ORIGINALS';
  if($('acknowledge').value!==phrase)throw new Error('Type the exact confirmation phrase shown in the review.');
  await run(reviewed.phase,{acknowledge:phrase,plan_sha256:reviewed.hash});$('review-dialog').close();
}));
safe(refresh)();
setInterval(()=>refresh().catch(error=>notice(error.message)),2000);
