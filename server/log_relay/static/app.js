'use strict';
const $=id=>document.getElementById(id);
let source=null, cursor=0, oldest=0, paused=false, generation=0;
const seen=new Set();
function state(text){$('status').textContent=text;}
async function api(path, data){
  const response=await fetch('api/'+path,{credentials:'same-origin',headers:data?{'Content-Type':'application/json'}:{},method:data?'POST':'GET',body:data?JSON.stringify(data):undefined});
  if(!response.ok){if(response.status===401) signOut();throw new Error(response.status===401?'请重新登录':'请求失败（'+response.status+'）');}
  return response.json();
}
function signOut(){generation++;if(source)source.close();source=null;$('login').hidden=false;document.querySelector('main').hidden=true;$('logout').hidden=true;state('请登录');}
function query(){const p=new URLSearchParams();for(const k of ['device','shop','task','level','q'])if($(k).value.trim())p.set(k,$(k).value.trim());return p;}
function render(events,prepend=false){
  const box=$('logs'), height=box.scrollHeight, fragment=document.createDocumentFragment();
  for(const e of events){if(seen.has(e.id))continue;seen.add(e.id);oldest=oldest?Math.min(oldest,e.id):e.id;
    const row=document.createElement('div');row.className='row '+(['ERROR','WARNING'].includes(e.level)?e.level:'INFO');row.dataset.id=e.id;
    const meta=document.createElement('div');meta.className='meta';meta.textContent=new Date(e.ts*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})+' · '+e.device.slice(0,8)+' · '+e.level+(e.shop?' · '+e.shop:'')+(e.task?' · '+e.task:'');
    const text=document.createElement('div');text.textContent=e.message;row.append(meta,text);fragment.append(row);
  }
  if(prepend)box.prepend(fragment);else box.append(fragment);
  while(box.children.length>1000){const row=prepend?box.lastChild:box.firstChild;seen.delete(Number(row.dataset.id));row.remove();}
  oldest=box.firstChild?Number(box.firstChild.dataset.id):0;
  if(prepend)box.scrollTop+=box.scrollHeight-height;else if($('follow').checked)box.scrollTop=box.scrollHeight;
}
function connect(){
  if(source)source.close();if(paused)return;
  const p=query();p.set('after',cursor);source=new EventSource('api/stream?'+p);
  source.onopen=()=>state('实时连接中');source.onerror=()=>state('连接中断，正在自动重连…');
  source.addEventListener('logs',e=>{const result=JSON.parse(e.data);render(result.events);cursor=result.cursor;state('实时更新 · '+new Date().toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai'}));});
  source.addEventListener('expired',()=>signOut());
}
async function reload(){
  const current=++generation;if(source)source.close();
  const result=await api('logs?'+query());if(current!==generation)return;
  $('logs').replaceChildren();seen.clear();oldest=0;render(result.events);cursor=result.cursor;connect();
}
async function devices(){
  const result=await api('devices'), selected=$('device').value;
  $('device').replaceChildren(new Option('所有电脑',''));
  for(const d of result.devices){const online=Date.now()/1000-d.seen<45;$('device').add(new Option(d.name+' · '+(online?'在线':'离线'),d.id));}
  $('device').value=selected;deviceNames=new Map(result.devices.map(d=>[d.id,d.name]));
}
async function enter(){await devices();$('login').hidden=true;document.querySelector('main').hidden=false;$('logout').hidden=true;await reload();}
function report(error){$('error-public').hidden=false;$('error-public').textContent=error.message;$('error').textContent=error.message;state(error.message);}
$('login').onsubmit=async e=>{e.preventDefault();try{await api('login',{password:$('password').value});$('password').value='';await enter();}catch(error){report(error);}};
$('logout').onclick=async()=>{try{await api('logout',{});}finally{signOut();}};
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'继续显示':'暂停显示';if(paused){if(source)source.close();state('已暂停，继续后从原位置补取日志');}else connect();};
$('history').onclick=async()=>{const current=generation;try{const p=query();if(oldest)p.set('before',oldest);const result=await api('logs?'+p);if(current===generation)render(result.events,true);}catch(error){report(error);}};

let deviceNames=new Map(), cachedOrders=[], cachedTasks=[], currentPage='overview', refreshVersion=0, followingToday=true;
const fmt=ts=>ts?new Date(ts*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
const todayBeijing=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
function node(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
function empty(text){return node('div',text,'empty');}
function businessQuery(){const p=new URLSearchParams();for(const k of ['device','shop','task'])if($(k).value.trim())p.set(k,$(k).value.trim());return p;}
function meter(value){const wrap=node('div',undefined,'progress-track');if(value==null){wrap.classList.add('pending');wrap.append(node('span','等待扫描队列'));}else{const bar=document.createElement('progress');bar.max=1;bar.value=Math.max(0,Math.min(1,Number(value)||0));bar.setAttribute('aria-label','处理进度');wrap.append(bar,node('strong',Math.round(bar.value*100)+'%'));}return wrap;}
function countCard(label,value,note){const c=node('div',undefined,'metric');c.append(node('span',label),node('strong',value),node('small',note||''));return c;}
function table(target,columns,rows){const t=document.createElement('table');const head=t.createTHead().insertRow();for(const [,label] of columns)head.append(node('th',label));const body=t.createTBody();for(const item of rows){const tr=body.insertRow();for(const [key] of columns)tr.insertCell().textContent=item[key]??'—';}$(target).replaceChildren(rows.length?t:empty('所选日期没有真实提交记录。'));}
function renderTasks(data){
 cachedTasks=data.tasks;
 const fragment=document.createDocumentFragment();let tasks=0;
 for(const t of data.tasks){if($('device').value&&t.device!==$('device').value||$('task').value&&t.task!==$('task').value)continue;
 const shops=t.shops.filter(s=>!$('shop').value.trim()||s.shop===$('shop').value.trim());if(!shops.length)continue;tasks++;
 const stale=Date.now()/1000-Math.min(t.updated,t.ts||t.updated)>45, failed=t.shops.filter(s=>s.failed).length;
 const box=node('article',undefined,'panel task-panel'),heading=node('div',undefined,'section-heading');
 const title=node('div');title.append(node('h3',(deviceNames.get(t.device)||t.device.slice(0,8))+' · '+t.task),node('p','开始 '+fmt(t.started)+' · 更新 '+fmt(t.ts||t.updated),'muted'));
 heading.append(title,node('span',stale?'数据已过期':t.finished?(failed?'任务结束 · 有异常':'任务已结束'):'正在执行','badge '+(stale?'warn':failed?'danger':t.finished?'success':'running')));box.append(heading);
 box.append(meter(t.progress));const cards=node('div',undefined,'cards compact');cards.append(countCard('结束店铺',t.shops.filter(s=>s.terminal).length+' / '+t.shops.length),countCard('已处理订单',t.processed||0,'包含操作失败的尝试'),countCard('异常店铺',failed),countCard('预计剩余',t.finished?'已结束':t.eta==null?'计算中':Math.ceil(t.eta/60)+' 分钟'));box.append(cards);
 const hint=node('p','总体进度按本轮分配店铺等权计算；任务结束不代表全部申诉成功。'+(stale?' 当前只显示上次上报，不能代表正在运行。':''),'muted');box.append(hint);
 const list=node('div',undefined,'shop-list');for(const s of shops){const row=node('div',undefined,'shop-row'+(s.failed?' has-error':''));row.append(node('strong',s.shop));
 const known=Number.isFinite(s.total)&&Number.isFinite(s.remaining), processed=known?Math.max(0,Math.min(s.total,s.total-s.remaining)):null;
 const value=known?(s.total>0?processed/s.total:s.terminal?1:0):null;
 const finishing=known&&s.total>0&&s.remaining===0&&!s.terminal&&!s.failed;
 const status=node('span',s.failed?'⚠ 异常':s.skipped_today?'今日已完成':s.terminal?'已结束':finishing?'正在收尾':String(s.status||'等待开始').split(/[｜|]/)[0],'badge '+(s.failed?'danger':s.terminal?'success':'running'));row.append(status);
 row.append(meter(value),node('span',s.skipped_today?'本轮无需重复处理':known?'已处理 '+processed+' / '+s.total+' · 待处理 '+s.remaining:s.checked?'已回查 '+s.checked+' 条':'队列数量待扫描','shop-count'),node('span',finishing?'订单已处理完，正在收尾':s.status||'等待开始','shop-step'));list.append(row);}box.append(list);fragment.append(box);
 }
 $('tasks').replaceChildren(tasks?fragment:empty('尚未收到当前任务进度。运行端更新并重启后，将显示本轮分配的店铺、队列及实时步骤。'));
}
function currentProcessingOrder(){
 const normalize=task=>task==='发起复议'?'复议':task;
 return cachedOrders.find(o=>{
  const last=[...o.events].reverse().find(e=>e.message.includes(o.order)&&!e.message.includes('[鼠标诊断]'));
  if(last&&new RegExp('订单(?:号)?[\\s:：\\[【]*'+o.order+'[：:]\\s*(?:已提交|成功|失败|分析失败|出错|跳过|[^\\n:：]{0,30}-跳过)').test(last.message))return false;
  return cachedTasks.some(t=>!t.finished&&Date.now()/1000-Math.min(t.updated,t.ts||t.updated)<=45&&t.device===o.device&&t.session_id===o.session_id&&normalize(t.task)===normalize(o.task)&&o.ts>=t.started&&t.shops.some(s=>s.shop===o.shop&&!s.terminal&&!s.failed&&String(s.status||'').includes(o.order)));
 });
}
function renderOrders(){const opened=new Set([...$('orders').querySelectorAll('details[open]')].map(d=>d.dataset.key));const fragment=document.createDocumentFragment(),current=currentProcessingOrder();let count=0;for(const o of cachedOrders){if($('errors-only').checked&&!o.has_error)continue;count++;const active=o===current,d=node('details',undefined,'order-row'+(o.has_error?' has-error':'')+(active?' is-current':''));d.dataset.key=JSON.stringify([o.device,o.session_id,o.shop,o.task,o.order]);if(active)d.setAttribute('aria-current','true');d.open=opened.has(d.dataset.key);const h=node('summary',undefined,'order-summary');
 const stamp=node('div',undefined,'order-time');stamp.append(node('strong',fmt(o.ts)),node('small','开始 '+fmt(o.first_ts)));
 const text=node('div',undefined,'order-main');text.append(node('strong',o.shop||'店铺未标记'),node('span',o.order+' · '+(o.task||'任务未标记')),node('p',o.message));
 const badges=node('div',undefined,'order-status');if(active)badges.append(node('span','正在处理','badge current-badge'));if(o.has_error)badges.append(node('span','⚠ 有错误记录','badge danger'));else if(!active)badges.append(node('span','● 处理动态','badge running'));if(o.screenshots&&o.screenshots.length)badges.append(node('span','有错误截图','badge running'));
 h.append(stamp,text,badges);d.append(h);
 if(o.last_error)d.append(node('p','最近错误：'+o.last_error,'error-reason'));
 if(o.screenshots&&o.screenshots.length){const gallery=node('div',undefined,'error-screenshots');gallery.append(node('strong','错误现场截图'));for(const shot of o.screenshots){const link=node('a');link.href=shot.url;link.target='_blank';link.rel='noopener noreferrer';link.title='打开原图 · '+fmt(shot.ts);const img=document.createElement('img');img.src=shot.url;img.alt='订单 '+o.order+' 错误现场截图';img.loading='lazy';link.append(img,node('small',fmt(shot.ts)+' · 点击查看原图'));gallery.append(link);}d.append(gallery);}
 const timeline=node('div',undefined,'timeline');for(const e of o.events){const line=node('div',undefined,e.level==='ERROR'?'timeline-error':'');line.append(node('time',fmt(e.ts)),node('span',(e.level==='ERROR'?'⚠ ':'')+e.message));timeline.append(line);}d.append(timeline);
 const b=node('button','下载诊断信息');b.onclick=()=>download('order-'+o.order+'.txt',JSON.stringify(o,null,2),'text/plain');d.append(b);fragment.append(d);}
 $('orders').replaceChildren(count?fragment:empty($('errors-only').checked?'当前筛选没有异常订单。':'当前筛选没有订单动态。'));
}
async function refreshOrders(version){const data=await api('orders?'+businessQuery());if(version!==refreshVersion)return;$('order-scope').textContent=data.scope;cachedOrders=data.orders;renderOrders();}
async function refreshReports(version){
 if(followingToday)$('start').value=$('end').value=todayBeijing();
 const p=businessQuery();p.delete('task');for(const k of ['start','end','kind'])if($(k).value)p.set(k,$(k).value);
 const data=await api('stats?'+p);if(version!==refreshVersion)return;
 const total=data.groups.reduce((sum,g)=>sum+g.total,0);
 const cards=[countCard(data.start===data.today&&data.end===data.today?'今日真实提交':'所选日期真实提交',total,'按已收到的提交记录统计'),...['负向申诉','复议','售后申诉'].map(kind=>countCard(kind,data.groups.filter(g=>g.kind===kind).reduce((a,g)=>a+g.total,0),'条真实提交'))];$('stat-cards').replaceChildren(...cards);
 const shops=new Map();
 for(const g of data.groups){const key=JSON.stringify([g.device,g.shop]);if(!shops.has(key))shops.set(key,{device_name:deviceNames.get(g.device)||g.device.slice(0,8),shop:g.shop,negative:0,reconsideration:0,aftersale:0,total:0});const row=shops.get(key),field={'负向申诉':'negative','复议':'reconsideration','售后申诉':'aftersale'}[g.kind];if(field){row[field]+=g.total;row.total+=g.total;}}
 table('summary',[['device_name','电脑'],['shop','店铺'],['negative','负向申诉'],['reconsideration','复议'],['aftersale','售后申诉'],['total','合计']],[...shops.values()]);
 $('report-freshness').textContent=data.sources.map(s=>(deviceNames.get(s.device)||s.device.slice(0,8))+' · '+(s.mode==='snapshot'?'同步 '+fmt(s.updated)+(Date.now()/1000-s.updated>120?' ⚠ 快照已过期':''):s.mode==='confirmed_logs'?'按日志确认的提交记录统计；完整快照尚未收到，数量可能不完整':'尚未收到提交记录；当前数量按已收到的数据计算')+(s.error?' · '+s.error:'')).join('；')||'尚未收到提交记录；当前数量按已收到的数据计算';
}
async function refreshDashboard(){const version=++refreshVersion;await devices();if(version!==refreshVersion)return;
 if(currentPage==='overview'){const data=await api('progress');if(version!==refreshVersion)return;renderTasks(data);await refreshOrders(version);}else if(currentPage==='statistics')await refreshReports(version);
 $('error-public').hidden=true;
}
function download(name,text,type){const url=URL.createObjectURL(new Blob([text],{type})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
for(const button of document.querySelectorAll('[data-page]'))button.onclick=()=>{currentPage=button.dataset.page;$('task').hidden=currentPage==='statistics';for(const page of document.querySelectorAll('.page'))page.hidden=page.id!==currentPage;for(const tab of document.querySelectorAll('[data-page]')){const selected=tab===button;tab.classList.toggle('active',selected);tab.setAttribute('aria-selected',String(selected));}refreshDashboard().catch(report);};
$('start').value=$('end').value=todayBeijing();
$('today').onclick=()=>{followingToday=true;$('start').value=$('end').value=todayBeijing();refreshDashboard().catch(report);};
$('start').onchange=$('end').onchange=()=>{followingToday=false;};
$('errors-only').onchange=renderOrders;
$('report-refresh').onclick=()=>refreshDashboard().catch(report);
$('apply').onclick=()=>Promise.all([reload(),refreshDashboard()]).catch(report);
$('log-apply').onclick=()=>reload().catch(report);
setInterval(()=>refreshDashboard().catch(report),5000);
enter().then(refreshDashboard).catch(report);
