/* Fault conditions and deduplication, with a controlled clock and no browser/hardware. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = path.resolve(__dirname, '../src/remote_teleop/web');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(web, 'i18n.js'), 'utf8').replaceAll('export const', 'const'), context);
vm.runInContext(fs.readFileSync(path.join(web, 'alerts.js'), 'utf8').replace('export class', 'class'), context);
const AlertCenter = vm.runInContext('AlertCenter', context);
const translations = vm.runInContext('strings.zh', context);
const t = key => translations[key] || key;
const status = () => ({connected:true,telemetry_age_ms:10,config_in_sync:true,healthy:true,
  mode:'DISARMED',reason:'DISARMED',epoch:'epoch',leader_error:null,
  recorder:{state:'IDLE',dataset:'episode',frames:0},video:{main:{age_ms:10}},record_job:null});
let now, center;
function reset() { now=1000;center=new AlertCenter(t,()=>{},()=>now); }
function latest() { return center.queue.at(-1); }
const checks=[];
function check(name, run) { reset();run();checks.push(name); }
check('normal wait and zero-frame recording do not look like faults',()=>{
  const s=status();s.mode='POLICY_ACTIVE';s.policy_inference_waiting=true;s.recorder.state='RECORDING';
  center.observe(s);assert.equal(center.queue.length,0);
});
check('HTTP 404 is precise and one notice per epoch',()=>{
  const s={...status(),mode:'SAFE_HOLD',reason:'POLICY_SERVICE_ERROR',policy_error:{error_type:'InvalidStatus',http_status:404}};
  for(let i=0;i<20;i++)center.observe(s);
  assert.equal(center.queue.length,1);assert.match(latest().message,/404/);assert.match(latest().recovery,/推理服务/);
  center.dismiss();center.observe(s);assert.equal(center.queue.length,0);
  s.epoch='another';center.observe(s);assert.equal(center.queue.length,1);
});
check('inference timeout includes the actual budgets',()=>{
  center.observe({...status(),mode:'SAFE_HOLD',reason:'POLICY_INFERENCE_TIMEOUT',policy_inference_budget_ms:500,command_transport_allowance_ms:250});
  assert.match(latest().detail,/500.*250/);
});
check('asynchronous recorder failure alerts',()=>{
  center.observe({...status(),record_job:{id:'save-1',state:'FAILED',operation:'record_finish',error:'DISK_SPACE_LOW',dataset:'episode'}});
  assert.equal(center.queue.length,1);assert.match(latest().recovery,/磁盘/);
});
check('old name errors do not interrupt a new recording',()=>{
  center.observe({...status(),record_job:{id:'old',state:'FAILED',operation:'record_start',error:'DATASET_EXISTS',dataset:'old'}},{datasetName:'new'});
  assert.equal(center.queue.length,0);
});
check('homing failure carries the feedback cause',()=>{
  center.observe({...status(),mode:'SAFE_HOLD',reason:'HOMING_FAILED',homing:{id:'home',target:'leader',state:'FAILED',error:'LEADER_OR_FEEDBACK_STALE'}});
  assert.equal(center.queue.length,1);assert.match(latest().detail,/反馈/);
});
check('persistent configuration mismatch and reoccurrence',()=>{
  const s={...status(),config_in_sync:false};center.observe(s);assert.equal(center.queue.length,0);
  now+=1501;center.observe(s);assert.equal(center.queue.length,1);center.dismiss();
  center.observe(s);assert.equal(center.queue.length,0);center.observe(status());
  center.observe(s);now+=1501;center.observe(s);assert.equal(center.queue.length,1);
});
check('short disconnect does not spam and persistent disconnect alerts',()=>{
  const s={...status(),connected:false};center.observe(s);now+=1000;center.observe(s);assert.equal(center.queue.length,0);
  now+=501;center.observe(s);assert.equal(latest().code,'OFFLINE');
});
check('stale cameras alert with names',()=>{
  const s={...status(),video:{main:{age_ms:2000}}};center.observe(s);now+=2001;center.observe(s);
  assert.equal(latest().code,'VIDEO_UNAVAILABLE');assert.match(latest().detail,/主视角/);
});
check('persistent feedback and robot health failures alert',()=>{
  const s={...status(),leader_error:'LEADER_OR_FEEDBACK_STALE',healthy:false};center.observe(s);now+=1501;center.observe(s);
  assert.equal(center.queue.length,2);
});
check('long recording jobs get one progress alert',()=>{
  const s={...status(),record_job:{id:'slow',state:'RUNNING',operation:'record_start'}};
  center.observe(s);now+=59999;center.observe(s);assert.equal(center.queue.length,0);
  now+=2;center.observe(s);assert.equal(latest().code,'RECORD_JOB_SLOW');
  center.observe(s);assert.equal(center.queue.length,1);
});
check('intentional pause and completed episodes do not alert',()=>{
  for(const reason of ['OPERATOR_PAUSE','EPISODE_FINISHED','HOMING_CANCELLED'])
    center.observe({...status(),mode:'SAFE_HOLD',reason});
  assert.equal(center.queue.length,0);
});
check('credential-bearing addresses never enter popup text',()=>{
  center.request({message:'failed wss://example.invalid/path?token=secret',code:'NETWORK_ERROR'});
  assert.doesNotMatch(JSON.stringify(center.queue),/secret|example\.invalid/);
});
check('manual errors are distinct and already-reported errors do not duplicate',()=>{
  center.request({message:'failed'});center.request({message:'failed'});
  assert.equal(center.queue.length,2);center.request({message:'failed',notified:true});assert.equal(center.queue.length,2);
  center.dismiss();assert.equal(center.queue.length,1);
});
check('recorder errors deduplicate until recovery',()=>{
  const s={...status(),recorder:{state:'ERROR',error:'RECORDING_QUEUE_FULL',dataset:'episode'}};
  center.observe(s);center.observe(s);assert.equal(center.queue.length,1);center.dismiss();
  center.observe(status());center.observe(s);assert.equal(center.queue.length,1);
});
check('notification queue is bounded',()=>{
  for(let i=0;i<100;i++)center.push(`error-${i}`,'REQUEST_FAILED');
  assert.equal(center.queue.length,32);
});
check('an open popup follows the selected language',()=>{
  let language='zh',rendered;
  const all=vm.runInContext('strings',context);
  const c=new AlertCenter(key=>all[language][key]||key,notice=>{rendered=notice;});
  c.observe({...status(),mode:'SAFE_HOLD',reason:'POLICY_SERVICE_ERROR',policy_error:{error_type:'InvalidStatus',http_status:404}});
  assert.match(rendered.message,/推理入口/);language='en';c.refresh();assert.match(rendered.message,/HTTP 404/);
  assert.match(rendered.recovery,/Check the policy service/);assert.equal(rendered.title,all.en.faultTitle);
});
console.log(JSON.stringify({passed:true,checks,hardware_opened:false}));
