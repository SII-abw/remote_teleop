/* Actual frontend recording flow against controlled API responses; no hardware. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const webRoot = process.env.TELEOP_WEB_ROOT || path.resolve(__dirname, '../src/remote_teleop/web');
class Element {
  constructor(tag) {
    this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.className='';
    this.textContent='';this.value='';this.disabled=false;
    this.classList={toggle:(name,force)=>{
      const values=new Set(this.className.split(/\s+/).filter(Boolean));
      (force===undefined?!values.has(name):force)?values.add(name):values.delete(name);
      this.className=[...values].join(' ');
    },add:name=>this.classList.toggle(name,true),remove:name=>this.classList.toggle(name,false),
    contains:name=>this.className.split(/\s+/).includes(name)};
  }
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=[...items];}
  removeAttribute(key){delete this[key];}
  closest(){return this;}
}
const elements=[];
const html=fs.readFileSync(path.join(webRoot,'index.html'),'utf8');
for(const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*)>/g)) {
  const element=new Element(match[1]);
  for(const attr of match[2].matchAll(/([a-z][a-z0-9-]*)="([^"]*)"/g)) {
    if(attr[1].startsWith('data-'))element.dataset[attr[1].slice(5).replace(/-([a-z])/g,(_,v)=>v.toUpperCase())]=attr[2];
    else if(attr[1]==='class')element.className=attr[2];
    else element[attr[1]]=attr[2];
  }
  elements.push(element);
}
const $=id=>elements.find(e=>e.id===id);
const document={body:new Element('body'),documentElement:{},getElementById:$,
  createElement:tag=>new Element(tag),addEventListener(){},
  querySelectorAll:selector=>elements.filter(e=>selector.startsWith('.')
    ?e.className.split(/\s+/).includes(selector.slice(1))
    :selector.startsWith('[data-')&&e.dataset[selector.slice(6,-1)]!==undefined)};
let status={connected:true,telemetry_age_ms:1,config_in_sync:true,mode:'READY',epoch:'epoch',
  operation_mode:'hil',recording_required_for_hil:true,recording_permitted:true,
  recorder:{state:'IDLE',frames:17,dataset:'previous'},record_job:null};
const names=new Map([
  ['episode',{valid:true,available:true,suggested_name:'episode'}],
  ['used',{valid:true,available:false,error:'DATASET_EXISTS',suggested_name:'used_001'}],
  ['used_001',{valid:true,available:true,suggested_name:'used_001'}],
  ['used_002',{valid:true,available:true,suggested_name:'used_002'}],
  ['fresh',{valid:true,available:true,suggested_name:'fresh'}],
  ['bad name',{valid:false,available:false,error:'INVALID_DATASET_NAME',suggested_name:'episode_001'}],
]);
let slowResolve;
const requests=[];
const context=vm.createContext({document,structuredClone,confirm:()=>true,
  localStorage:{getItem(){return 'zh';},setItem(){}},
  setTimeout:()=>1,clearTimeout(){},setInterval(){},
  fetch:async(url,request)=>{
    requests.push({url,method:request.method,body:request.body?JSON.parse(request.body):null});
    let data;
    if(url==='/api/status') data=structuredClone(status);
    else if(url.startsWith('/api/recording/name?')){
      const name=new URL('http://fixture'+url).searchParams.get('name');
      data=name==='slow'?await new Promise(resolve=>{slowResolve=resolve;}):{name,...names.get(name)};
    } else if(url==='/api/control/record_start'){
      status.record_job={id:'start-job',state:'QUEUED',operation:'record_start',dataset:JSON.parse(request.body).name};
      data=structuredClone(status.record_job);
    } else if(url==='/api/control/record_finish'){
      status.mode='SAFE_HOLD';
      status.record_job={id:'finish-job',state:'QUEUED',operation:'record_finish',dataset:status.recorder.dataset};
      data=structuredClone(status.record_job);
    } else throw Error('Unexpected API request: '+url);
    return {ok:true,json:async()=>data};
  }});
const evaluate=source=>vm.runInContext(source,context);
evaluate(fs.readFileSync(path.join(webRoot,'i18n.js'),'utf8').replaceAll('export const','const'));
evaluate(fs.readFileSync(path.join(webRoot,'alerts.js'),'utf8').replace('export class','class'));
evaluate(fs.readFileSync(path.join(webRoot,'app.js'),'utf8').replace(/^import .*\n/gm,''));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const controlStart=elements.find(e=>e.dataset.control==='start');
const failure=elements.find(e=>e.dataset.outcome==='failure');
const starts=()=>requests.filter(r=>r.url==='/api/control/record_start').length;
async function name(value){$('dataset-name').value=value;$('dataset-name').oninput();await evaluate('checkRecordName()');}
(async()=>{
  await flush();
  assert.equal(controlStart.disabled,true,'HIL task requires recording');
  assert.match($('recorded-frames-label').textContent,/上次/);
  await name('used');
  assert.equal($('record-start').disabled,true);
  assert.match($('record-guidance').textContent,/此名称已被使用/);
  assert.equal($('record-name-suggestion').className.includes('hidden'),false);
  await $('record-start').onclick();
  assert.equal(starts(),0,'collision blocked before mutation');
  $('record-name-suggestion').onclick();await flush();
  assert.equal($('dataset-name').value,'used_001');
  assert.equal($('record-start').disabled,false);
  await $('record-start').onclick();
  assert.equal(starts(),1);
  assert.equal($('record-start').disabled,true,'QUEUED blocks duplicate start');
  assert.equal($('dataset-name').disabled,true);
  assert.match($('record-guidance').textContent,/初始化/);
  await $('record-start').onclick();
  assert.equal(starts(),1);
  status.recorder={state:'RECORDING',frames:0,dataset:'used_001'};
  status.record_job.state='COMPLETE';
  await evaluate('poll()');await flush();
  assert.match($('record-guidance').textContent,/等待运动数据/);
  assert.equal(controlStart.disabled,false);
  assert.equal(failure.disabled,false);
  await failure.onclick();
  assert.equal(failure.disabled,true);
  assert.match($('record-guidance').textContent,/正在保存/);
  names.set('used_001',{valid:true,available:false,error:'DATASET_EXISTS',suggested_name:'used_002'});
  status.recorder={state:'IDLE',frames:3,dataset:'used_001'};
  status.record_job.state='COMPLETE';
  await evaluate('poll()');await flush();
  assert.equal($('dataset-name').value,'used_002','completion prepares next name');
  assert.equal($('record-start').disabled,false);
  status.record_job={id:'old-failure',state:'FAILED',operation:'record_start',dataset:'used_001',error:'DATASET_EXISTS'};
  await evaluate('poll()');await flush();
  assert.equal($('record-start').disabled,false,'old name error does not block a fresh name');
  assert.doesNotMatch($('record-detail').textContent,/已存在/);
  $('dataset-name').value='slow';const slow=evaluate('checkRecordName()');await flush();
  await name('fresh');
  slowResolve({name:'slow',valid:true,available:false,suggested_name:'slow_001',error:'DATASET_EXISTS'});
  await slow;await flush();
  assert.equal(evaluate('recordNameStatus.name'),'fresh','late response cannot override newer input');
  assert.equal($('record-start').disabled,false);
  await name('bad name');
  assert.equal($('record-start').disabled,true);
  await $('record-start').onclick();assert.equal(starts(),1);
  await name('fresh');
  status.connected=false;await evaluate('poll()');
  assert.equal($('record-start').disabled,true);
  assert.match($('record-guidance').textContent,/未连接/);
  status.connected=true;status.mode='READY';status.recording_required_for_hil=false;
  await evaluate('poll()');await flush();
  assert.equal(controlStart.disabled,false,'optional recording mode remains optional');
  status.recording_required_for_hil=true;
  await evaluate('poll()');assert.equal(controlStart.disabled,true);
  status.mode='POLICY_ACTIVE';status.reason='POLICY_READY';status.policy_inference_waiting=true;
  await evaluate('poll()');assert.match($('reason').textContent,/等待策略推理/);
  status.config_in_sync=false;
  await evaluate('poll()');assert.doesNotMatch($('reason').textContent,/等待策略推理/);
  status.config_in_sync=true;status.policy_inference_waiting=false;
  status.mode='SAFE_HOLD';status.reason='POLICY_INFERENCE_TIMEOUT';
  await evaluate('poll()');assert.match($('reason').textContent,/推理与传输预算/);
  console.log(JSON.stringify({passed:true,checks:['collision and suggestion','no duplicate queued operations',
    'recording without motion feedback','save progress','automatic next name','stale error isolation',
    'out-of-order name responses','invalid name','disconnect reason','HIL recording prerequisite',
    'normal inference wait','configuration fault priority','specific inference timeout'],hardware_opened:false}));
})().catch(error=>{console.error(error);process.exitCode=1;});
