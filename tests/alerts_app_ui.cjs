/* Actual app handlers: async failures, expired login, stale polling and urgent control. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const web=path.resolve(__dirname,'../src/remote_teleop/web');
class Element {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.className='';this.textContent='';this.value='';this.disabled=false;
    this.classList={toggle:(name,force)=>{const all=new Set(this.className.split(/\s+/).filter(Boolean));(force===undefined?!all.has(name):force)?all.add(name):all.delete(name);this.className=[...all].join(' ');},
      add:name=>this.classList.toggle(name,true),remove:name=>this.classList.toggle(name,false),contains:name=>this.className.split(/\s+/).includes(name)};}
  append(...items){this.children.push(...items);} replaceChildren(...items){this.children=[...items];}
  removeAttribute(name){delete this[name];} closest(){return this;} focus(){} scrollIntoView(){}
}
const elements=[],listeners=new Map(),intervals=[];
for(const match of fs.readFileSync(path.join(web,'index.html'),'utf8').matchAll(/<([a-z][a-z0-9]*)\b([^>]*)>/g)){
  const element=new Element(match[1]);
  for(const attr of match[2].matchAll(/([a-z][a-z0-9-]*)="([^"]*)"/g)){
    if(attr[1].startsWith('data-'))element.dataset[attr[1].slice(5).replace(/-([a-z])/g,(_,v)=>v.toUpperCase())]=attr[2];
    else if(attr[1]==='class')element.className=attr[2];else element[attr[1]]=attr[2];
  } elements.push(element);
}
const $=id=>elements.find(element=>element.id===id);
const document={body:new Element('body'),documentElement:{},getElementById:$,createElement:tag=>new Element(tag),
  addEventListener:(type,fn)=>{if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(fn);},
  querySelectorAll:selector=>elements.filter(element=>selector.startsWith('.')?element.className.split(/\s+/).includes(selector.slice(1)):
    selector.startsWith('[data-')&&element.dataset[selector.slice(6,-1)]!==undefined)};
let clock=1000,pendingPoll=null,holdPoll=false,failNames=false;
let status={connected:true,telemetry_age_ms:10,config_in_sync:true,healthy:true,mode:'SAFE_HOLD',reason:'POLICY_SERVICE_ERROR',epoch:'fault-1',
  operation_mode:'hil',recording_required_for_hil:true,recording_permitted:true,velocity_limits_enabled:false,
  recorder:{state:'RECORDING',dataset:'episode_002',frames:0},policy_error:{error_type:'InvalidStatus',http_status:404},
  video:{main:{age_ms:10},left_hand:{age_ms:10},right_hand:{age_ms:10}}};
const requests=[];
const response=(data,code=200)=>({ok:code<400,status:code,json:async()=>data});
const context=vm.createContext({document,structuredClone,confirm:()=>true,Date:class extends Date{static now(){return clock;}},
  localStorage:{getItem:()=> 'zh',setItem(){}},setTimeout:()=>1,clearTimeout(){},setInterval:fn=>intervals.push(fn),
  fetch:async(url,request)=>{
    requests.push({url,method:request.method,body:request.body?JSON.parse(request.body):null});
    if(url==='/api/status'){
      if(holdPoll){holdPoll=false;return new Promise(resolve=>{pendingPoll=()=>resolve(response(structuredClone(status)));});}
      return response(structuredClone(status));
    }
    if(url.startsWith('/api/recording/name?')){
      if(failNames)throw Error('network unavailable');
      const name=new URL('http://fixture'+url).searchParams.get('name');return response({name,valid:true,available:true,suggested_name:name});
    }
    if(url==='/api/login')return response({ok:true});
    if(url==='/api/bad-auth')return response({error:'AUTH_REQUIRED'},401);
    if(url==='/api/bad-body')return {ok:false,status:502,json:async()=>{throw Error('not json');}};
    if(url==='/api/control/arm')return response({error:'HEALTH_CHECK_FAILED'},400);
    if(url==='/api/control/toggle'){status.mode='HUMAN_ACTIVE';status.reason='HUMAN_READY';status.epoch='human';return response({ok:true});}
    if(url==='/api/control/estop'){status.mode='ESTOP_LATCHED';status.reason='OPERATOR_ESTOP';status.epoch='stopped';return response({ok:true});}
    throw Error('Unexpected request: '+url);
  }});
const evaluate=code=>vm.runInContext(code,context),flush=()=>new Promise(resolve=>setImmediate(resolve));
evaluate(fs.readFileSync(path.join(web,'i18n.js'),'utf8').replaceAll('export const','const'));
evaluate(fs.readFileSync(path.join(web,'alerts.js'),'utf8').replace('export class','class'));
evaluate(fs.readFileSync(path.join(web,'app.js'),'utf8').replace(/^import .*\n/gm,''));
function dismissAll(){while(evaluate('alerts.queue.length'))$('fault-dismiss').onclick();}
function key(key){for(const handler of listeners.get('keydown')||[])handler({key,target:{tagName:'BUTTON'},preventDefault(){},repeat:false,ctrlKey:false,metaKey:false,altKey:false});}
const count=url=>requests.filter(request=>request.url===url).length;
const checks=[];
(async()=>{
  await flush();await flush();
  assert.match($('fault-message').textContent,/404/);assert.match($('record-guidance').textContent,/故障暂停/);
  assert.equal($('dataset-name').value,'episode_002','reload keeps the active recording name');
  assert.equal($('velocity-readiness').classList.contains('hidden'),false);checks.push('specific policy popup and paused recording guidance');
  const before=requests.length;dismissAll();await evaluate('poll()');await flush();
  assert.equal($('fault-dialog').classList.contains('hidden'),true);assert.equal(status.mode,'SAFE_HOLD');
  assert.equal(requests.slice(before).filter(request=>request.url.startsWith('/api/control/')).length,0);checks.push('acknowledging never resets or starts motion');
  status={...status,mode:'POLICY_ACTIVE',reason:'POLICY_READY',epoch:'active',policy_error:null};await evaluate('poll()');
  evaluate('alerts.push("takeover-fixture","NETWORK_ERROR"); busy=true');
  key('s');await flush();assert.equal(count('/api/control/record_finish'),0,'popup prevents accidental dataset shortcuts');
  key('i');await flush();await flush();assert.equal(count('/api/control/toggle'),1,'takeover bypasses unrelated busy request');
  assert.equal(status.mode,'HUMAN_ACTIVE');checks.push('takeover remains available with popup and busy request');
  await $('fault-stop').onclick();await flush();assert.equal(count('/api/control/estop'),1);checks.push('stop remains available inside popup');
  evaluate('busy=false');dismissAll();
  await evaluate('perform(()=>api("/control/arm","POST",{}))');assert.match($('fault-message').textContent,/健康|检查|HEALTH_CHECK_FAILED/);
  assert.equal(evaluate('busy'),false);checks.push('button errors create popup and release busy state');dismissAll();
  await evaluate('perform(()=>api("/bad-body"))');assert.match($('fault-message').textContent,/无效响应/);checks.push('non-JSON HTTP failures are visible');dismissAll();
  await evaluate('perform(()=>api("/bad-auth"))');assert.equal(evaluate('authenticated'),false);
  assert.equal($('login-overlay').classList.contains('hidden'),false);assert.equal(evaluate('alerts.queue.length'),1);
  assert.match($('reason').textContent,/登录|认证|AUTH_REQUIRED/);checks.push('expired session clears stale status and reports once');dismissAll();
  status={...status,mode:'READY',reason:'ARMED',epoch:'ready',recorder:{state:'IDLE',frames:0,dataset:null},policy_error:null};
  await $('login-form').onsubmit({preventDefault(){}});await flush();await flush();
  failNames=true;await $('record-start').onclick();assert.match($('fault-message').textContent,/录制名称/);
  assert.equal(count('/api/control/record_start'),0);checks.push('name preflight failure explains blocked recording');failNames=false;dismissAll();
  await evaluate('poll()');await flush();dismissAll();
  holdPoll=true;const waiting=evaluate('poll()');await flush();const pollCount=count('/api/status');
  clock+=2000;for(const callback of intervals)callback();await flush();
  assert.equal(count('/api/status'),pollCount,'polls do not overlap');assert.match($('fault-message').textContent,/状态更新已中断/);
  assert.equal($('control-mode').textContent,'未连接');assert.equal($('fault-stop').disabled,false);
  assert.ok(pendingPoll);pendingPoll();await waiting;checks.push('hung polling becomes unknown state without overlapping requests');
  console.log(JSON.stringify({passed:true,checks,hardware_opened:false}));
})().catch(error=>{console.error(error);process.exitCode=1;});
