// Local UI verification. Uses an isolated Edge profile and fictional prototype data only.
import {spawn} from "node:child_process";
import {mkdtemp, readFile, mkdir, writeFile, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {setTimeout as delay} from "node:timers/promises";

const root=path.dirname(fileURLToPath(import.meta.url));
const out=path.resolve(root,"../docs/meetly-logs");
const profile=await mkdtemp(path.join(tmpdir(),"meetly-qa-"));
const browser=process.env.MEETLY_BROWSER || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const child=spawn(browser,["--headless","--disable-gpu","--no-first-run","--no-default-browser-check",`--user-data-dir=${profile}`,"--remote-debugging-port=0","about:blank"],{windowsHide:true,stdio:"ignore"});
const report={date:new Date().toISOString(),checks:[],errors:[]};
let ws,sequence=0;const pending=new Map();
let launchError;child.on("error",(error)=>{launchError=error;});
const rpc=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error(`CDP timeout: ${method}`));},12000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
const evaluate=async(expression)=>{const r=await rpc("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(r.exceptionDetails.text+": "+r.exceptionDetails.exception?.description);return r.result.value;};
const check=async(name,expression)=>{if(!await evaluate(expression))throw new Error(`FAIL: ${name}`);report.checks.push({name,result:"PASS"});};
const snapshot=async(name,width,height,mobile)=>{await rpc("Emulation.setDeviceMetricsOverride",{width,height,deviceScaleFactor:1,mobile});await delay(150);await check(`${name}: no horizontal overflow`,"document.documentElement.scrollWidth <= innerWidth");const image=await rpc("Page.captureScreenshot",{format:"png",captureBeyondViewport:true});await writeFile(path.join(out,`${name}.png`),Buffer.from(image.data,"base64"));};
try{
  await mkdir(out,{recursive:true});
  let port;
  for(let n=0;n<100;n++){if(launchError)throw launchError;try{port=Number((await readFile(path.join(profile,"DevToolsActivePort"),"utf8")).split("\n")[0]);break;}catch{await delay(100);}}
  if(!port)throw new Error("Browser debugging endpoint unavailable");
  const pages=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws=new WebSocket(pages.find((p)=>p.type==="page").webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  ws.onmessage=({data})=>{const message=JSON.parse(data);if(message.method==="Runtime.exceptionThrown")report.errors.push(message.params.exceptionDetails.text);const p=pending.get(message.id);if(p){clearTimeout(p.timer);pending.delete(message.id);message.error?p.reject(new Error(message.error.message)):p.resolve(message.result);}};
  await rpc("Runtime.enable");await rpc("Page.enable");
  await rpc("Page.navigate",{url:pathToFileURL(path.join(root,"index.html")).href});
  for(let n=0;n<50;n++){if(await evaluate("Boolean(document.querySelector('[data-time]'))"))break;await delay(100);}
  await check("calendar loads", "Boolean(document.querySelector('[data-time]'))");
  await snapshot("desktop-calendar",1440,1100,false);
  await snapshot("mobile-calendar",390,1000,true);
  await snapshot("narrow-calendar",320,1000,true);
  await evaluate("document.querySelector('[data-time]').click();document.querySelector('#continue').click()");
  await check("booking form opens", "document.querySelector('#modal').open && Boolean(document.querySelector('[name=title]'))");
  await evaluate("document.querySelector('[name=title]').value='<img src=x onerror=alert(1)>';document.querySelector('[name=comment]').value='Тестовый комментарий';document.querySelector('#booking-form').requestSubmit()");
  await check("review escapes user content", "Boolean(document.querySelector('#send-booking')) && !document.querySelector('#modal-content img')");
  await evaluate("document.querySelector('#edit-booking').click();document.querySelector('[name=title]').value='Первая консультация';document.querySelector('#booking-form').requestSubmit()");
  await snapshot("mobile-booking-review",390,950,true);
  await evaluate("document.querySelector('#send-booking').click()");
  await check("booking appears in client list", "document.querySelectorAll('[data-record]').length===3");
  await evaluate("document.querySelector('#role').value='owner';document.querySelector('#role').dispatchEvent(new Event('change'));document.querySelector('[data-nav=list]').click()");
  await check("owner sees incoming requests", "document.querySelectorAll('[data-record]').length===3");
  await evaluate("document.querySelector('[data-record=\"4\"]').click();document.querySelector('#approve').click();document.querySelector('#role').value='client';document.querySelector('#role').dispatchEvent(new Event('change'));document.querySelector('[data-nav=list]').click();document.querySelector('[data-record=\"4\"]').click()");
  await check("owner approval visible to client", "Boolean(document.querySelector('#move'))");
  await evaluate("document.querySelector('#move').click();document.querySelector('[data-time]').click();document.querySelector('#continue').click();document.querySelector('#booking-form').requestSubmit();document.querySelector('#send-booking').click();document.querySelector('[data-record=\"4\"]').click()");
  await check("reschedule keeps original date visible", "document.querySelector('#modal-content').textContent.includes('До подтверждения действует прежнее время') && !document.querySelector('#move')");
  await evaluate("document.querySelector('#cancel').click()");
  await check("cancellation asks for confirmation", "Boolean(document.querySelector('#decision-form'))");
  await evaluate("document.querySelector('#decision-form').requestSubmit()");
  await check("cancelled record removed from active list", "!document.querySelector('[data-record=\"4\"]')");
  await evaluate("document.querySelector('[data-filter=all]').click();document.querySelector('[data-record=\"4\"]').click()");
  await check("cancelled record remains in history", "document.querySelector('#modal-content .status').textContent==='Отменена' && !document.querySelector('#cancel')");
  await evaluate("document.querySelector('#close-modal').click();document.querySelector('#role').value='owner';document.querySelector('#role').dispatchEvent(new Event('change'));document.querySelector('[data-nav=list]').click();document.querySelector('[data-record=\"3\"]').click();document.querySelector('#reject').click();document.querySelector('[name=reason]').value='Выберите другое время';document.querySelector('#decision-form').requestSubmit()");
  await check("rejected request leaves incoming list", "!document.querySelector('[data-record=\"3\"]')");
  await evaluate("document.querySelector('[data-nav=calendar]').click()");
  await snapshot("desktop-owner",1440,1100,false);
  if(report.errors.length)throw new Error("Browser reported JavaScript errors");
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
