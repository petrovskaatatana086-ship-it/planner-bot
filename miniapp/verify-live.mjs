// Local UI verification. Uses an isolated Edge profile and fictional prototype data only.
import {spawn} from "node:child_process";
import {mkdtemp, readFile, mkdir, writeFile, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {setTimeout as delay} from "node:timers/promises";

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),"../miniapp");
const out=path.resolve(root,process.env.MEETLY_QA_OUTPUT||"../docs/meetly-logs/miniapp-live");
const profile=await mkdtemp(path.join(tmpdir(),"meetly-qa-"));
const browser=process.env.MEETLY_BROWSER || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const child=spawn(browser,["--headless","--disable-gpu","--no-first-run","--no-default-browser-check",`--user-data-dir=${profile}`,"--remote-debugging-port=0","about:blank"],{windowsHide:true,stdio:"ignore"});
const report={date:new Date().toISOString(),checks:[],errors:[]};
let ws,sequence=0;const pending=new Map();
let launchError;child.on("error",(error)=>{launchError=error;});
const rpc=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error(`CDP timeout: ${method}`));},12000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
const evaluate=async(expression)=>{const r=await rpc("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(r.exceptionDetails.text+": "+r.exceptionDetails.exception?.description);return r.result.value;};
const check=async(name,expression)=>{if(!await evaluate(expression))throw new Error(`FAIL: ${name}`);report.checks.push({name,result:"PASS"});};
const snapshot=async(name,width,height,mobile)=>{await rpc("Emulation.setDeviceMetricsOverride",{width,height,deviceScaleFactor:1,mobile});await delay(150);await check(`${name}: no horizontal overflow`,`document.documentElement.scrollWidth <= ${width} && innerWidth === ${width}`);const image=await rpc("Page.captureScreenshot",{format:"png",captureBeyondViewport:true});await writeFile(path.join(out,`${name}.png`),Buffer.from(image.data,"base64"));};
try{
  await mkdir(out,{recursive:true});
  let port;
  for(let n=0;n<100;n++){if(launchError)throw launchError;try{port=Number((await readFile(path.join(profile,"DevToolsActivePort"),"utf8")).split("\n")[0]);break;}catch{await delay(100);}}
  if(!port)throw new Error("Browser debugging endpoint unavailable");
  const pages=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws=new WebSocket(pages.find((p)=>p.type==="page").webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  ws.onmessage=({data})=>{const message=JSON.parse(data);if(message.method==="Runtime.exceptionThrown")report.errors.push(message.params.exceptionDetails.exception?.description||message.params.exceptionDetails.text);const p=pending.get(message.id);if(p){clearTimeout(p.timer);pending.delete(message.id);message.error?p.reject(new Error(message.error.message)):p.resolve(message.result);}};
  await rpc("Runtime.enable");await rpc("Page.enable");

  await rpc("Network.enable");
  await rpc("Network.setBlockedURLs",{urls:["https://telegram.org/*"]});
  const wait=async(expression)=>{for(let n=0;n<100;n++){if(await evaluate(expression))return;await delay(100);}throw new Error("UI wait timed out: "+expression);};
  const login=async(id)=>{
    const {initData}=await(await fetch("http://127.0.0.1:18713/qa/"+id)).json();
    if(globalThis.scriptId)await rpc("Page.removeScriptToEvaluateOnNewDocument",{identifier:globalThis.scriptId});
    const result=await rpc("Page.addScriptToEvaluateOnNewDocument",{source:"window.Telegram={WebApp:{initData:"+JSON.stringify(initData)+",ready(){},expand(){}}};"});
    globalThis.scriptId=result.identifier;
    await rpc("Page.navigate",{url:"http://127.0.0.1:18713/miniapp/"});
  };
  await login(101);
  await wait("Boolean(document.querySelector('[data-slot]'))");
  await check("verified client identity and no role selector","document.querySelector('#account-role').textContent==='Клиент'&&!document.querySelector('#role')");
  await snapshot("client-calendar-desktop",1440,1100,false);
  await snapshot("client-calendar-mobile",390,1000,true);
  await snapshot("client-calendar-narrow",320,1000,true);
  await evaluate("document.querySelector('[data-slot]').click();document.querySelector('#continue').click();document.querySelector('[name=title]').value='UI test <script>';document.querySelector('#booking-form').requestSubmit()");
  await check("review before sending","document.querySelector('#send')&&document.querySelector('#modal-content').textContent.includes('UI test <script>')&&!document.querySelector('#modal-content script')");
  await evaluate("document.querySelector('#send').click()");
  await wait("Boolean(document.querySelector('[data-record]'))&&!document.querySelector('#modal').open");
  await check("request persisted on server","document.querySelector('#workspace').textContent.includes('Ожидает подтверждения')");
  await login(777);
  await wait("Boolean(document.querySelector('[data-nav=inbox]'))");
  await evaluate("document.querySelector('[data-nav=inbox]').click()");
  await wait("Boolean(document.querySelector('[data-record]'))");
  await snapshot("owner-inbox-mobile",390,1000,true);
  await evaluate("document.querySelector('[data-record]').click()");
  await wait("Boolean(document.querySelector('#approve'))");
  await evaluate("document.querySelector('#approve').click()");
  await wait("!document.querySelector('#modal').open&&document.querySelector('#workspace').textContent.includes('Здесь пока нет')");
  await evaluate("document.querySelector('[data-nav=list]').click()");
  await wait("Boolean(document.querySelector('[data-record]'))");
  await evaluate("document.querySelector('[data-record]').click()");
  await wait("Boolean(document.querySelector('#move'))");
  await check("owner sees meeting actions","Boolean(document.querySelector('#cancel'))");
  await evaluate("document.querySelector('#move').click()");
  await wait("Boolean(document.querySelector('[data-slot]'))");
  await check("reschedule offers server slots","document.querySelector('#workspace').textContent.includes('Выберите новое время')");
  await evaluate("document.querySelector('[data-slot]').click();document.querySelector('#continue').click();document.querySelector('#booking-form').requestSubmit();document.querySelector('#send').click()");
  await wait("!document.querySelector('#modal').open&&Boolean(document.querySelector('[data-record]'))");
  await evaluate("document.querySelector('[data-record]').click()");
  await wait("Boolean(document.querySelector('#cancel'))");
  await check("owner direct reschedule saved","document.querySelector('#modal-content').textContent.includes('Перенесена')");
  await evaluate("document.querySelector('#cancel').click()");
  await wait("Boolean(document.querySelector('#decision'))");
  await check("cancellation asks confirmation","document.querySelector('#decision').textContent.includes('Да, отменить')");
  await evaluate("document.querySelector('#decision').requestSubmit()");
  await wait("!document.querySelector('#modal').open&&Boolean(document.querySelector('[data-record]'))");
  await check("cancelled status loaded","document.querySelector('#workspace').textContent.includes('Отменена специалистом')");
  await evaluate("document.querySelector('[data-nav=settings]').click()");
  await wait("Boolean(document.querySelector('#settings-form'))");
  await evaluate("document.querySelector('[name=default_duration]').value='45';document.querySelector('#settings-form').requestSubmit()");
  await wait("Boolean(document.querySelector('[name=default_duration]'))&&document.querySelector('[name=default_duration]').value==='45'&&!document.querySelector('[type=submit]').disabled");
  await snapshot("owner-settings-narrow",320,1000,true);
  await login(303);
  await wait("Boolean(document.querySelector('#register'))");
  await check("new client must register","!document.querySelector('[data-slot]')");
  await evaluate("document.querySelector('[name=full_name]').value='Synthetic new client';document.querySelector('[name=email]').value='synthetic@example.com';document.querySelector('[name=consent]').checked=true;document.querySelector('#register').requestSubmit()");
  await wait("Boolean(document.querySelector('[data-slot]'))");
  await check("registered client sees calendar","document.querySelector('#identity-name').textContent==='Synthetic new client'");
  if(report.errors.length)throw new Error("Browser JavaScript errors");
  report.status="PASS";
}catch(error){report.status="FAIL";report.failure=error.message;process.exitCode=1;}
finally{
  for(const p of pending.values())clearTimeout(p.timer);
  if(ws?.readyState===WebSocket.OPEN){try{await rpc("Browser.close");}catch{}ws.close();}
  child.kill();await delay(500);
  // profile was created by mkdtemp directly inside the OS temporary directory.
  if(path.dirname(profile)===tmpdir()&&path.basename(profile).startsWith("meetly-qa-"))await rm(profile,{recursive:true,force:true,maxRetries:3}).catch(()=>{});
  await mkdir(out,{recursive:true});await writeFile(path.join(out,"ui-verification.json"),JSON.stringify(report,null,2)+"\n");
  console.log(JSON.stringify(report,null,2));
}
