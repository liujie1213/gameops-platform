'use strict';
const $ = id => document.getElementById(id);
const titles = {overview:'工作台', reports:'举报管理', matches:'局后判定', penalties:'处罚记录', policy:'规则配置', audit:'审计中心', words:'聊天黑白库', chat:'聊天审核记录', games:'游戏接入'};
const statusNames = {pending:'待游戏执行',failed:'执行失败',succeeded:'已执行',expired:'已到期'};
const decisionNames = {clear:'未命中',hit:'命中并生成处罚',observed:'观察命中（未处罚）',disabled:'规则停用',out_of_scope:'模式不适用'};
const fields = {id:'记录 ID',event_id:'事件 ID',player_id:'玩家',reporter_id:'举报人',match_id:'对局',category:'举报类型',occurred_at:'发生时间',at:'记录时间',game_type:'游戏模式',kd:'本局 KD',kill_num:'击杀数',average_rank:'平均等级',level:'玩家等级',report_count:'24h 举报量',decision:'判定结果',hits:'命中条件',command_id:'处罚指令',policy_version:'策略版本',action:'处罚动作',status:'执行状态',created_at:'创建时间',expires_at:'冻结至',duration_seconds:'处罚时长（秒）',message:'游戏回执',summary:'记录摘要',kind:'类别',details:'详细内容'};
Object.assign(fields,{allowed_by:'放行白库词条',text:'聊天消息',enforcement:'策略执行模式',list_type:'所属词库'});
const state = {games:[], game:'', page:'overview', data:null, generation:0, sid:0};
const base='/api/admin/games';
const scoped=(game,section)=>base+'/'+encodeURIComponent(game)+'/'+section;
const esc=value=>String(value??'').replace(/[&<>"']/g, char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const when=sec=>sec?new Date(Number(sec)*1000).toLocaleString('zh-CN',{hour12:false}):'—';
const mode=id=>({2:'普通爆破',5:'排位爆破'}[id]||'模式 '+id);
const groups=ids=>ids?.length?ids.map(id=>'第'+id+'组').join('、'):'—';
const badge=(str,tone='')=>'<span class="badge '+tone+'">'+esc(str)+'</span>';
const go=(key,title,primary=false)=>'<button class="'+(primary?'primary':'secondary')+'" data-go="'+key+'">'+title+'</button>';
const action=(act,label,index)=>'<button class="link" data-action="'+act+'" data-index="'+index+'">'+label+'</button>';
const panel=(title,contents,control='')=>'<article class="panel"><div class="panel-head"><h2>'+title+'</h2>'+control+'</div><div class="panel-body">'+contents+'</div></article>';
function table(headings,rows,empty='暂无记录'){
  return '<div class="scroll"><table><thead><tr>'+headings.map(h=>'<th scope="col">'+h+'</th>').join('')+'</tr></thead><tbody>'+(rows.length?rows.map(cells=>'<tr>'+cells.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join(''):'<tr><td class="empty" colspan="'+headings.length+'">'+esc(empty)+'</td></tr>')+'</tbody></table></div>';
}
function info(message,success=false){$('notice').hidden=!message;$('notice').classList.toggle('ok',success);$('notice').textContent=message}
function modal(title,contents){$('modal-title').textContent=title;$('modal-body').innerHTML=contents;if(!$('modal').open)$('modal').showModal()}
function showLogin(message=''){
  state.generation++;state.sid++;state.game='';state.games=[];state.data=null;state.page='overview';
  if($('modal').open)$('modal').close();$('shell').hidden=true;$('login').hidden=false;$('view').replaceChildren();$('login-error').textContent=message;$('login-form').elements.password.focus();
}
async function api(path,body){
  const res=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  let data;try{data=await res.json()}catch{throw Error('服务返回异常，请检查后台服务后刷新。')}
  if(!res.ok){if(res.status===401&&path!=='/api/login'&&!$('shell').hidden)showLogin('登录已过期，请重新登录。');throw Error(data.error||'操作失败（'+res.status+'）')}
  return data;
}
function gameSelector(){
  $('game').replaceChildren();for(const item of state.games)$('game').add(new Option(item.name,item.id));
  if(!state.games.length)$('game').add(new Option('尚未接入游戏',''));$('game').disabled=!state.games.length;$('game').value=state.game;
}
function pageChrome(){
  const current=state.games.find(g=>g.id===state.game);$('page-title').textContent=titles[state.page];$('crumb').textContent=titles[state.page];
  $('scope').textContent=state.page==='games'?'数据范围：全部游戏':'数据范围：'+(current?.name||'尚未接入游戏');
  document.querySelectorAll('nav [data-go]').forEach(b=>{if(b.dataset.go===state.page){b.setAttribute('aria-current','page');const group=b.closest('details');if(group)group.open=true}else b.removeAttribute('aria-current')});
}
async function navigate(page=state.page,game=state.game,refreshGames=false){
  if(!Object.hasOwn(titles,page))return;const epoch=++state.generation;state.page=page;state.game=game;state.data=null;
  document.body.classList.remove('mobile-open');pageChrome();info('');$('view').innerHTML='<p class="help">正在读取记录…</p>';
  try{
    if(refreshGames||page==='games'){
      const reply=await api(base);if(epoch!==state.generation)return;
      state.games=reply.items;state.game=state.games.find(g=>g.id===game)?.id||state.games[0]?.id||'';gameSelector();pageChrome();
    }
    if(page==='games'){renderGames();return}
    if(!state.game){$('view').innerHTML=panel('接入第一款游戏','<p class="help">创建游戏后即可获取专属接入密钥。新游戏默认仅观察：记录规则命中，不下发冻结指令。</p>'+go('games','创建游戏',true));return}
    const data=await api(scoped(state.game,page));if(epoch!==state.generation)return;state.data=data;renderSection(page,data);
  }catch(error){if(epoch===state.generation){$('view').replaceChildren();info(error.message)}}
}
function renderGames(){
  $('view').innerHTML=panel('新增游戏','<form id="create-game" class="toolbar"><label>游戏名称<input name="name" maxlength="60" required></label><button class="primary">创建游戏并签发密钥</button></form>')+
    panel('已接入游戏',table(['游戏','游戏 ID','创建时间','操作'],state.games.map((g,i)=>[esc(g.name),esc(g.id),when(g.created_at),action('open-game','进入工作台',i)+' '+action('rotate','轮换密钥',i)]),'暂无游戏'))+
    panel('服务端接入','<ol class="guide"><li>创建一款独立的测试游戏，将本次签发的密钥保存在该游戏服务端。新游戏默认仅观察，规则命中不会产生处罚指令。</li><li>如要完整演练冻结和回执，先在顶部选中这款测试游戏，前往「规则配置」，把「命中后的动作」切换为「执行处罚」并发布。仅验证举报和规则命中时，可保留观察模式。</li><li>双击项目目录中的 <code>game-client.cmd</code>，按提示输入测试游戏密钥。</li><li>在「局后判定」核对命中原因；游戏回传执行结果后，在「处罚记录」查看回执。</li></ol><p class="after">接口地址 <code>'+esc(location.origin)+'/api/v1/</code>　<a href="/integration/guide" target="_blank" rel="noopener">打开游戏接入指南 ↗</a>　<a href="/integration/openapi.json" download="gameops-openapi.json">下载 OpenAPI 接口契约</a></p>');
}
function penaltyRows(items,fromOverview){
  return table(['时间','被处罚玩家','对局','命中组','处罚动作','执行状态','操作'],items.map((e,i)=>[when(e.created_at),esc(e.player_id),esc(e.match_id),esc(groups(e.hits)),'封号 6 小时',badge(statusNames[e.status]||e.status,({pending:'blue',failed:'red',succeeded:'green'}[e.status])),action('detail','详情',i)+(e.status==='failed'&&!fromOverview?' '+action('retry','重试',i):'')]),'暂无处罚指令');
}
function renderOverview(d){
  const metrics=[['累计举报',d.report_count],['局后判定次数',d.match_count],['累计处罚指令',d.penalty_count],['累计拦截消息',d.block_count],['行为策略',!d.enabled?'已停用':d.enforcement==='observe'?'仅观察':'执行处罚']];
  return '<div class="stats">'+metrics.map(([k,v])=>'<div class="metric"><span>'+k+'</span><strong>'+esc(v)+'</strong></div>').join('')+'</div>'+
    panel('快捷入口','<div class="buttons">'+go('policy','配置规则',true)+go('penalties','查看处罚记录')+go('words','管理词库')+'</div>')+
    panel('最近处罚触发（Top 10）',penaltyRows(d.recent.slice(0,10),true),'<button class="link" data-go="penalties">查看全部 →</button>');
}
function renderWords(d){
  return panel('新增或更新词条','<p class="help">白库仅在整条消息完全匹配时优先放行；其余消息进入黑库匹配。同一词条更改所属词库并保存后，旧归属会被替换。</p><form id="save-word" class="toolbar"><label>词条文本<input name="term" required minlength="1" maxlength="128"></label><label>所属词库<select name="list_type"><option value="block">黑库 · 拦截</option><option value="allow">白库 · 放行</option></select></label><label>匹配方式<select name="match_type"><option value="contains">包含匹配</option><option value="exact">完全匹配</option></select></label><button class="primary">保存并生效</button></form>')+
    panel('游戏聊天黑白库',table(['词条','所属词库','匹配方式','状态','命中次数','操作'],d.items.map((w,i)=>[esc(w.term),w.list_type==='allow'?badge('白库','green'):badge('黑库'),w.match_type==='exact'?'完全匹配':'包含匹配',badge(w.enabled?'生效':'已停用',w.enabled?'blue':''),esc(w.hits),action('toggle',w.enabled?'停用':'启用',i)]),'暂无词条，可以先添加一个用于联调的测试词。'));
}
function renderPolicy(p){
  const numberCell=(r,key,opt=false)=>'<input class="rule-cell" name="r'+r.id+'-'+key+'" aria-label="第'+r.id+'组 '+({reports:'举报数',level:'等级上限',kd:'KD',kills:'击杀数',avg:'平均等级'}[key])+'" type="number" min="0" step="'+(['kd','avg'].includes(key)?'any':'1')+'" value="'+esc(r[key])+'" '+(opt?'placeholder="不限"':'required')+'>';
  return panel('高举报异常战绩冻结（局后）','<form id="publish-policy"><div class="toolbar"><label>策略状态<select name="enabled"><option value="1" '+(p.enabled?'selected':'')+'>启用</option><option value="0" '+(!p.enabled?'selected':'')+'>停用</option></select></label><label>命中后的动作<select name="enforcement"><option value="observe" '+(p.enforcement==='observe'?'selected':'')+'>仅观察，不下发处罚</option><option value="enforce" '+(p.enforcement!=='observe'?'selected':'')+'>执行处罚</option></select></label><label>24 小时举报统计<select name="report_mode"><option value="events" '+(p.report_mode==='events'?'selected':'')+'>举报事件数</option><option value="reporters" '+(p.report_mode==='reporters'?'selected':'')+'>去重举报人数</option></select></label><label>计入的举报类型<input name="report_category" placeholder="留空计入全部类型" maxlength="60" value="'+esc(p.report_category)+'"></label></div><p class="help">仅检测普通爆破和排位爆破。同组条件全部满足即命中。仅观察时记录命中组，不创建处罚；执行处罚时，每次对局判定最多生成一条封号 6 小时的指令。</p>'+
  table(['组','24h 举报 ≥','等级 ≤','本局 KD ≥','击杀数 ≥','平均等级 ≥'],p.rules.map(r=>['第 '+esc(r.id)+' 组',numberCell(r,'reports'),numberCell(r,'level',true),numberCell(r,'kd'),numberCell(r,'kills'),numberCell(r,'avg',true)]))+'<div class="buttons after"><button class="primary">发布规则</button><span class="muted">发布后用于新到达的对局。</span></div></form>',badge('版本 '+p.version,'blue'));
}
function renderSection(page,d){
  let html='';
  if(page==='overview')html=renderOverview(d);
  else if(page==='words')html=renderWords(d);
  else if(page==='chat')html=panel('近期聊天审核',table(['时间','玩家','消息','结果','命中词条','操作'],d.items.map((e,i)=>[when(e.occurred_at),esc(e.player_id),esc(String(e.text??'').slice(0,80)),badge(e.decision==='block'?'黑库拦截':e.allowed_by?'白库放行':'已放行',e.decision==='block'?'amber':'green'),esc(e.allowed_by||e.hits.join('、')||'—'),action('detail','详情',i)])));
  else if(page==='policy')html=renderPolicy(d);
  else if(page==='reports')html=panel('游戏上报的举报',table(['举报时间','被举报玩家','举报人','类型','操作'],d.items.map((e,i)=>[when(e.occurred_at),esc(e.player_id),esc(e.reporter_id),esc(e.category)||'—',action('detail','详情',i)])));
  else if(page==='matches')html=panel('对局结算判定',table(['结算时间','玩家 / 对局','模式','24h 举报','KD / 击杀','结果','操作'],d.items.map((e,i)=>[when(e.occurred_at),esc(e.player_id)+' / '+esc(e.match_id),esc(mode(e.game_type)),esc(e.report_count),esc(e.kd)+' / '+esc(e.kill_num),badge(decisionNames[e.decision]||e.decision,e.decision==='hit'?'amber':e.decision==='observed'?'blue':''),action('detail','详情',i)])));
  else if(page==='penalties')html=panel('游戏处罚指令',penaltyRows(d.items,false));
  else if(page==='audit')html=panel('近期事件与配置变更',table(['记录时间','摘要','操作'],d.items.map((e,i)=>[when(e.at),esc(e.summary),action('detail','详情',i)])));
  $('view').innerHTML=html;
}
function detailValue(key,value){
  if(value===null||value===undefined||value==='')return '—';
  if(['occurred_at','at','created_at','expires_at'].includes(key))return esc(when(value));
  if(key==='game_type')return esc(mode(value));if(key==='status')return esc(statusNames[value]||value);if(key==='decision')return esc(decisionNames[value]||({block:'已拦截',allow:'已放行'}[value])||value);
  if(key==='hits'&&Array.isArray(value))return esc(value.every(v=>typeof v==='number')?groups(value):value.join('、')||'—');
  if(key==='action'&&value==='freeze')return '封号';
  return typeof value==='object'?'<pre>'+esc(JSON.stringify(value,null,2))+'</pre>':esc(value);
}
function showKey(gameName,token){
  modal('游戏密钥 · '+gameName,'<p>此密钥仅在签发时显示一次。请保存到对应游戏服务端。</p><pre id="game-key">'+esc(token)+'</pre><button class="primary" data-action="copy">复制密钥</button> <span id="copy-result" role="status"></span>');
}
function describe(record){modal('记录详情','<dl>'+Object.entries(record).map(([k,v])=>'<dt>'+esc(fields[k]||k)+'</dt><dd>'+detailValue(k,v)+'</dd>').join('')+'</dl>')}
async function applyChange(section,body,success){
  const {generation,game,page}=state;await api(scoped(game,section),body);if(generation!==state.generation)return;await navigate(page,game);if(state.game===game&&state.page===page&&!$('notice').textContent)info(success,true);
}
async function clickAction(el){
  const i=Number(el.dataset.index),act=el.dataset.action;const entries=state.data?.items??state.data?.recent;
  if(act==='copy'){
    const key=$('game-key');try{await navigator.clipboard.writeText(key.textContent);$('copy-result').textContent='已复制'}catch{const range=document.createRange();range.selectNodeContents(key);const sel=getSelection();sel.removeAllRanges();sel.addRange(range);$('copy-result').textContent='请按 Ctrl+C 复制'}return;
  }
  if(act==='open-game'){const g=state.games[i];$('game').value=g.id;return navigate('overview',g.id)}
  if(act==='rotate'){
    const g=state.games[i],sid=state.sid;if(!confirm('立即更换「'+g.name+'」的接入密钥？旧密钥将失效，游戏端需要同步更新。'))return;
    const res=await api(scoped(g.id,'rotate'),{});if(sid===state.sid)showKey(g.name,res.token);return;
  }
  if(!entries?.[i])return;const item=entries[i];
  if(act==='detail')describe(item);
  else if(act==='toggle')await applyChange('words',{term:item.term,list_type:item.list_type||'block',match_type:item.match_type,enabled:!item.enabled},'词条状态已更新。');
  else if(act==='retry')await applyChange('penalties',{action:'retry',command_id:item.command_id},'已重新进入待执行队列。');
}
async function submitForm(form){
  const d=new FormData(form);
  if(form.id==='login-form'){
    await api('/api/login',{password:d.get('password')});form.reset();return enter();
  }
  if(form.id==='create-game'){
    const sid=state.sid;const name=String(d.get('name')).trim();if(!name)throw Error('请填写游戏名称。');
    const g=await api(base,{name});if(sid!==state.sid)return;showKey(g.name||name,g.token);return navigate('games',g.id,true);
  }
  if(form.id==='save-word')return applyChange('words',{term:d.get('term').trim(),list_type:d.get('list_type'),match_type:d.get('list_type')==='allow'?'exact':d.get('match_type'),enabled:true},'词条已保存。');
  if(form.id==='publish-policy'){
    const current=state.data;const rules=current.rules.map(r=>{
      const next={id:r.id};for(const key of ['reports','level','kd','kills','avg']){const value=d.get('r'+r.id+'-'+key);next[key]=value===''?null:Number(value)}return next;
    });
    return applyChange('policy',{version:current.version,enabled:d.get('enabled')==='1',enforcement:d.get('enforcement'),report_mode:d.get('report_mode'),report_category:d.get('report_category').trim(),rules},'新规则已发布。');
  }
}
async function run(button,work,login=false){
  if(button?.disabled)return;if(button)button.disabled=true;const epoch=state.generation;
  try{await work()}catch(error){if(login)$('login-error').textContent=error.message;else if(epoch===state.generation&&!$('shell').hidden)info(error.message)}finally{if(button)button.disabled=false}
}
async function enter(){state.sid++;$('login').hidden=true;$('shell').hidden=false;await navigate('overview','',true)}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.go)navigate(b.dataset.go);else if(b.dataset.action)run(b,()=>clickAction(b))});
document.addEventListener('submit',e=>{e.preventDefault();run(e.submitter,()=>submitForm(e.target),e.target.id==='login-form')});
document.addEventListener('change',e=>{if(!e.target.matches('#save-word select[name="list_type"]'))return;const white=e.target.value==='allow',match=e.target.form.elements.match_type;if(white)match.value='exact';match.disabled=white});
$('game').onchange=()=>navigate(state.page,$('game').value);$('reload').onclick=()=>navigate(state.page,state.game,true);
$('dismiss').onclick=()=>$('modal').close();$('modal').onclose=()=>$('modal-body').replaceChildren();
$('menu').onclick=()=>document.body.classList.toggle(matchMedia('(max-width:780px)').matches?'mobile-open':'closed');$('shade').onclick=()=>document.body.classList.remove('mobile-open');
$('logout').onclick=()=>run($('logout'),async()=>{await api('/api/logout',{});showLogin()});
(async()=>{
  const ticket=new URLSearchParams(location.hash.slice(1)).get('login');if(ticket)history.replaceState(null,'',location.pathname+location.search);
  try{if(ticket){await api('/api/login',{password:ticket});return enter()}if((await api('/api/session')).authenticated)return enter();showLogin()}catch(error){showLogin(error.message)}
})();
