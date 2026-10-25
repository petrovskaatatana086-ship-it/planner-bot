"use strict";
(() => {
  const $ = s => document.querySelector(s);
  const esc = v => String(v ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const base = new URL("../api/meetly/v1/", location.href);
  const tg = window.Telegram?.WebApp;
  const state = {token:null, me:null, view:"calendar", month:null, day:null, time:null, moving:null, rules:null, days:[], slots:[], records:[], cursor:null, filter:"upcoming", ownerFilter:"all", busy:false, generation:0};
  const labels = {pending:"Ожидает подтверждения",approved:"Согласована",rejected:"Отклонена",withdrawn:"Снята",confirmed:"Подтверждена",rescheduled:"Перенесена",reschedule_pending:"Перенос на согласовании",cancelled_by_owner:"Отменена специалистом",cancelled_by_client:"Отменена клиентом"};
  const owner = () => state.me?.role === "owner";
  const key = () => crypto.randomUUID();
  const moscow = (value, opts={}) => new Intl.DateTimeFormat("ru-RU",{timeZone:"Europe/Moscow",...opts}).format(new Date(value));
  const dayKey = value => new Intl.DateTimeFormat("en-CA",{timeZone:"Europe/Moscow",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date(value));
  const today = dayKey(new Date());
  const dateLabel = value => moscow(value.length===10?value+"T12:00:00+03:00":value,{day:"numeric",month:"long"});
  const timeLabel = value => moscow(value,{hour:"2-digit",minute:"2-digit"});
  const line = (name,value) => `<div class="detail-line"><span>${esc(name)}</span><strong>${esc(value)}</strong></div>`;
  const button = (id,text,style="primary") => `<button id="${id}" class="${style}">${esc(text)}</button>`;
  function toast(text){$("#toast").textContent=text;$("#toast").classList.add("visible");setTimeout(()=>$("#toast").classList.remove("visible"),6500);}
  function errorBox(error){const box=$("#form-error");if(box)box.textContent=error.message;else toast(error.message);}
  async function api(path,{method="GET",body,id,version,auth}={}){
    const headers={Authorization:auth||`Bearer ${state.token}`};
    if(body!==undefined)headers["Content-Type"]="application/json";
    if(id)headers["Idempotency-Key"]=id;
    if(version)headers["If-Match"]=`"${version}"`;
    let response;
    try{response=await fetch(new URL(path,base),{method,headers,body:body===undefined?undefined:JSON.stringify(body),cache:"no-store",credentials:"omit"});}
    catch{throw new Error("Нет связи с сервером. Проверьте интернет и повторите действие.");}
    if(response.status===204)return null;
    let data;try{data=await response.json();}catch{throw new Error("Сервер временно недоступен. Повторите позже.");}
    if(!response.ok){const e=new Error(data.error?.message||"Не удалось выполнить действие.");e.code=data.error?.code;
      if(response.status===401){state.token=null;$("#navigation").replaceChildren();$("#workspace").innerHTML='<section class="panel"><h2>Войдите заново</h2><p>Закройте приложение и откройте его кнопкой в Telegram.</p></section>';$("#modal").close();}
      throw e;}
    return data.data;
  }
  function showModal(html){$("#modal-content").innerHTML=html+'<p id="form-error" class="inline-error" role="alert"></p>';if(!$("#modal").open)$("#modal").showModal();}
  function closeModal(){if(!state.busy)$("#modal").close();}
  async function mutate(el,task,success){if(state.busy)return;state.busy=true;el.disabled=true;try{await task();$("#modal").close();toast(success);state.moving=null;state.time=null;await render();}catch(e){errorBox(e);}finally{state.busy=false;el.disabled=false;}}
  function nav(){const entries=owner()?[["calendar","Календарь"],["list","Заявки"],["inbox","Входящие"],["settings","Настройки"]]:[["calendar","Записаться"],["list","Мои записи"]];
    $("#navigation").innerHTML=entries.map(([v,t])=>`<button class="nav-button ${state.view===v?"active":""}" data-nav="${v}">${t}</button>`).join("");
    document.querySelectorAll("[data-nav]").forEach(el=>el.onclick=()=>{state.moving=null;state.time=null;state.cursor=null;state.view=el.dataset.nav;render();});}
  async function render(){if(!state.token)return;nav();const generation=++state.generation;$("#workspace").innerHTML='<section class="panel loading">Загружаем…</section>';
    try{if(state.me.registration_required){await registration();return;}
      if(state.view==="calendar")await calendar(generation);
      else if(state.view==="settings")await settings();
      else await records(false,generation);
    }catch(e){if(generation!==state.generation||!state.token)return;$("#workspace").innerHTML=`<section class="panel"><p class="inline-error" role="alert">${esc(e.message)}</p>${button("reload","Повторить")}</section>`;$("#reload").onclick=render;}}
  async function registration(){const consent=await api("consent");
    $("#workspace").innerHTML=`<section class="panel registration-panel"><p class="tiny-label">ЗНАКОМИМСЯ</p><h2>Ваш профиль</h2><p class="consent-text">${esc(consent.text)}</p><form id="register"><label class="form-field">Имя<input name="full_name" required maxlength="255" value="${esc(state.me.profile.full_name)}" autocomplete="name"></label><label class="form-field">Email для приглашений<input type="email" name="email" required maxlength="254" autocomplete="email"></label><label class="booking-toggle"><span>Принимаю согласие на обработку данных</span><input type="checkbox" name="consent" required></label><p class="muted">На этом шаге владение email не проверяется.</p><p id="register-error" class="inline-error" role="alert"></p><button class="primary full-width" type="submit">Продолжить</button></form></section>`;
    let command=null,last=null;$("#register").onsubmit=async e=>{e.preventDefault();if(state.busy)return;const form=new FormData(e.target),body={full_name:String(form.get("full_name")).trim(),email:String(form.get("email")).trim(),consent:form.get("consent")==="on",consent_version:consent.version};
      const serialized=JSON.stringify(body);if(last!==serialized){command=key();last=serialized;}const el=e.submitter||e.target.querySelector("button[type=submit]");el.disabled=true;state.busy=true;
      try{await api("registration",{method:"POST",body,id:command});state.me=await api("me");identity();await render();}catch(error){$("#register-error").textContent=error.message;}finally{el.disabled=false;state.busy=false;}};}
  function monthRange(){const d=state.month||new Date(today+"T12:00:00Z"),y=d.getUTCFullYear(),m=d.getUTCMonth();
    return [new Date(Date.UTC(y,m,1,12)).toISOString().slice(0,10),new Date(Date.UTC(y,m+1,0,12)).toISOString().slice(0,10)];}
  async function calendar(generation){
    const [first,last]=monthRange();const moving=state.moving;
    const data=owner()&&!moving?await api(`admin/calendar?from=${first}&to=${last}&limit=100`):await api(`availability/days?from=${first}&to=${last}${moving?"&meeting_id="+moving.id:""}`);
    if(generation!==state.generation)return;
    if(owner()&&!moving){state.records=data.items;state.cursor=data.next_cursor;}else{state.days=data.days;state.settingsVersion=data.settings_version;}
    const start=new Date(first+"T12:00:00Z"),end=new Date(last+"T12:00:00Z");const blanks=(start.getUTCDay()+6)%7;
    const days=Array.from({length:end.getUTCDate()},(_,i)=>first.slice(0,8)+String(i+1).padStart(2,"0"));
    const available=new Map((data.days||[]).map(x=>[x.date,x.available_slots_count]));
    if(!state.day?.startsWith(first.slice(0,7)))state.day=owner()&&!moving?today:days.find(x=>available.get(x)>0)||null;
    $("#workspace").innerHTML=`<div class="workspace-grid"><section class="panel"><div class="panel-heading"><h2>${moscow(first+"T12:00:00Z",{month:"long",year:"numeric"})}</h2><div class="calendar-controls">${button("prev","‹","secondary")}${button("next","›","secondary")}</div></div>${moving?`<p class="note">Выберите новое время для встречи «${esc(moving.title)}».</p>`:""}<div class="weekdays">${["Пн","Вт","Ср","Чт","Пт","Сб","Вс"].map(x=>`<span>${x}</span>`).join("")}</div><div class="days">${'<span></span>'.repeat(blanks)}${days.map(d=>`<button class="day ${available.get(d)>0?"available":""} ${state.day===d?"selected":""}" data-day="${d}" ${owner()&&!moving?"":!available.get(d)?"disabled":""}>${Number(d.slice(-2))}</button>`).join("")}</div><p class="muted live-status">Московское время · свободное время проверяется перед записью</p></section><section id="day-panel" class="panel"></section></div>`;
    for(const [id,delta] of [["prev",-1],["next",1]])$("#"+id).onclick=()=>{state.month=new Date(Date.UTC(start.getUTCFullYear(),start.getUTCMonth()+delta,1,12));state.day=null;state.time=null;render();};
    document.querySelectorAll("[data-day]").forEach(el=>el.onclick=()=>{state.day=el.dataset.day;state.time=null;document.querySelectorAll("[data-day]").forEach(b=>b.classList.toggle("selected",b===el));renderDay();});
    await renderDay();}
  function card(r){const m=r.meeting||r,dt=m.start_at||r.start_at,person=r.client?.full_name||m.client?.full_name;
    return `<article class="meeting-row" tabindex="0" role="button" data-record="${r.kind||"booking"}:${r.id}"><div class="date-tile"><strong>${moscow(dt,{day:"numeric"})}</strong>${moscow(dt,{month:"short"})}</div><div class="meeting-info"><span class="status ${esc(m.status)}">${esc(labels[m.status]||m.status)}</span><h3>${esc(r.title||m.title)}</h3><p>${timeLabel(dt)} МСК${owner()&&person?" · "+esc(person):""}</p></div><span aria-hidden="true">↗</span></article>`;}
  function bindCards(){document.querySelectorAll("[data-record]").forEach(el=>{el.onclick=()=>openRecord(el.dataset.record);el.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();el.click();}};});}
  async function renderDay(){const panel=$("#day-panel"),day=state.day;if(!panel)return;
    if(!day){panel.innerHTML='<h2>Свободных дат нет</h2><p class="muted">Попробуйте другой месяц или уточните расписание у специалиста.</p>';return;}
    if(owner()&&!state.moving){const items=state.records.filter(r=>dayKey((r.meeting||r).start_at)===day);
      panel.innerHTML=`<h2>${dateLabel(day)}</h2>${items.length?items.map(card).join(""):'<p class="muted">На этот день записей нет.</p>'}${state.cursor?'<p class="muted">Показаны первые 100 записей месяца. Остальные доступны в разделе «Заявки».</p>':""}`;bindCards();return;}
    panel.innerHTML='<p>Проверяем свободное время…</p>';
    try{const data=await api(`availability/slots?date=${day}${state.moving?"&meeting_id="+state.moving.id:""}`);if(day!==state.day||!panel.isConnected)return;
      state.slots=data.slots;state.settingsVersion=data.settings_version;
      panel.innerHTML=`<p class="tiny-label">${state.moving?"НОВОЕ ВРЕМЯ":"ВРЕМЯ ДЛЯ ВАС"}</p><h2>${dateLabel(day)}</h2><p class="muted">${data.duration_minutes} минут · МСК</p><div class="slots">${data.slots.map((slot,i)=>`<button class="slot" data-slot="${i}">${timeLabel(slot.start_at)}</button>`).join("")}</div>${!data.slots.length?'<p class="muted">Свободного времени нет. Выберите другую дату.</p>':""}${button("continue",state.moving?"Продолжить перенос":"Записаться на встречу")} ${state.moving?button("stop-move","Отменить выбор","secondary"):""}`;
      $("#continue").disabled=true;document.querySelectorAll("[data-slot]").forEach(el=>el.onclick=()=>{state.time=data.slots[Number(el.dataset.slot)];document.querySelectorAll("[data-slot]").forEach(b=>b.classList.toggle("selected",b===el));$("#continue").disabled=false;});
      $("#continue").onclick=bookingForm;if($("#stop-move"))$("#stop-move").onclick=()=>{state.moving=null;state.view="list";render();};
    }catch(e){panel.innerHTML=`<p class="inline-error" role="alert">${esc(e.message)}</p>`;}}
  function bookingForm(values={}){const moving=state.moving,slot=state.time,settingsVersion=state.settingsVersion;if(!slot)return;
    showModal(`<p class="modal-eyebrow">${moving?"ПЕРЕНОС ВСТРЕЧИ":"НОВАЯ ЗАЯВКА"}</p><h2 class="modal-title" id="modal-title">${moving?"Новое удобное время":"О чём поговорим?"}</h2>${line("Дата",dateLabel(slot.start_at))}${line("Время",timeLabel(slot.start_at)+"–"+timeLabel(slot.end_at)+" МСК")}<form id="booking-form">${moving?"":`<label class="form-field">Тема встречи<input name="title" minlength="3" maxlength="100" required value="${esc(values.title)}"></label>`}<label class="form-field">Комментарий · необязательно<textarea name="comment" maxlength="2000">${esc(values.comment)}</textarea></label><button class="primary full-width" type="submit">Проверить</button></form>`);
    $("#booking-form").onsubmit=e=>{e.preventDefault();const form=new FormData(e.target),values={title:moving?moving.title:String(form.get("title")).trim(),comment:String(form.get("comment")).trim()};
      const body={start_at:slot.start_at,settings_version:settingsVersion};if(moving)body[owner()?"reason":"comment"]=values.comment;else Object.assign(body,values);
      const command=key(),path=moving?`${owner()?"admin/":""}meetings/${moving.id}/${owner()?"reschedule":"reschedule-requests"}`:"requests";
      showModal(`<p class="modal-eyebrow">ПРОВЕРЬТЕ ДАННЫЕ</p><h2 class="modal-title" id="modal-title">Всё верно?</h2>${line("Дата",dateLabel(slot.start_at))}${line("Время",timeLabel(slot.start_at)+" МСК")}<p class="safe-text">${esc(values.title)}</p><p class="safe-text muted">${esc(values.comment)}</p><p class="note">${moving?(owner()?"Время изменится сразу, клиент получит уведомление.":"До решения специалиста прежнее время остаётся в силе."):"Специалист рассмотрит вашу заявку. Уведомление придёт в Telegram."}</p><div class="actions">${button("edit","Изменить","secondary")}${button("send","Отправить")}</div>`);
      $("#edit").onclick=()=>bookingForm(values);$("#send").onclick=e=>mutate(e.target,async()=>{await api(path,{method:"POST",body,id:command,version:moving?.version});state.view="list";},moving?"Перенос сохранён":"Заявка отправлена");};}
  async function records(append,generation=state.generation){let path;
    if(owner())path=state.view==="inbox"?"admin/inbox?kind=all":"admin/requests?status="+state.ownerFilter;
    else path="records?view="+state.filter;
    const data=await api(path+"&limit=20"+(append&&state.cursor?"&cursor="+encodeURIComponent(state.cursor):""));if(generation!==state.generation)return;
    state.records=append?state.records.concat(data.items):data.items;state.cursor=data.next_cursor;
    let summary="";if(owner()){const stats=await api("admin/statistics");if(generation!==state.generation)return;summary=`<div class="request-summary">${[["all","Всего",stats.total],["approved","Согласовано",stats.approved],["rejected","Отклонено",stats.rejected],["pending","Ожидают",stats.pending]].map(([value,label,count])=>`<button class="stat-card ${state.view==="list"&&state.ownerFilter===value?"active":""}" aria-pressed="${state.view==="list"&&state.ownerFilter===value}" data-status="${value}"><span>${label}</span><strong>${count}</strong></button>`).join("")}</div>`;}
    $("#workspace").innerHTML=summary+`<section class="panel list-panel" id="records-panel" tabindex="-1"><h2>${state.view==="inbox"?"Новые заявки и переносы":owner()?"Заявки":"Мои заявки и встречи"}</h2>${!owner()?`<div class="filters"><button class="filter ${state.filter==="upcoming"?"active":""}" data-view="upcoming">Предстоящие</button><button class="filter ${state.filter==="history"?"active":""}" data-view="history">История</button></div>`:""}${state.records.length?state.records.map(r=>card(r.kind==="reschedule"?{...r.meeting,id:r.proposal.id,kind:"reschedule",title:"Перенос: "+r.meeting.title}:r)).join(""):'<div class="empty">Здесь пока нет записей.</div>'}${state.cursor?button("more","Показать ещё","secondary"):""}</section>`;
    bindCards();document.querySelectorAll("[data-status]").forEach(el=>el.onclick=async()=>{state.ownerFilter=el.dataset.status;state.view="list";state.cursor=null;const rendering=render(),generation=state.generation;await rendering;if(generation!==state.generation)return;const panel=$("#records-panel");if(panel){panel.focus({preventScroll:true});panel.scrollIntoView({block:"start",behavior:"instant"});}});
    document.querySelectorAll("[data-view]").forEach(el=>el.onclick=()=>{state.filter=el.dataset.view;render();});if($("#more"))$("#more").onclick=()=>records(true).catch(errorBox);}
  async function openRecord(identity){try{const [kind,value]=identity.split(":"),id=Number(value);const record=state.records.find(r=>(r.kind||"booking")===kind&&(r.kind==="reschedule"?r.proposal.id:r.id)===id);if(!record)return;
    let r;if(record.kind==="reschedule")r=await api(`admin/meetings/${record.meeting.id}`);else r=await api(`${owner()?"admin/":""}requests/${record.id}`);
    const m=r.meeting||("booking_request_id" in r?r:null),main=m||r,actions=main.allowed_actions||[];
    showModal(`<p class="modal-eyebrow">${owner()?esc((r.client||main.client)?.full_name):"МОЯ ЗАПИСЬ"} · № ${main.id}</p><h2 class="modal-title" id="modal-title">${esc(main.title)}</h2><span class="status ${esc(main.status)}">${esc(labels[main.status]||main.status)}</span>${line("Дата",dateLabel(main.start_at))}${line("Время",timeLabel(main.start_at)+"–"+timeLabel(main.end_at)+" МСК")}${owner()&&r.client?.email?line("Email",r.client.email):""}<p class="safe-text muted">${esc(r.comment||"")}</p>${main.pending_reschedule?`<p class="note">Предложено: ${dateLabel(main.pending_reschedule.proposed_start_at)}, ${timeLabel(main.pending_reschedule.proposed_start_at)} МСК</p>`:""}${main.last_reschedule_decision?.reason?`<p class="safe-text muted">Решение по переносу: ${esc(main.last_reschedule_decision.reason)}</p>`:""}<p class="safe-text muted">${esc(main.cancellation_reason||r.rejection_reason||"")}</p>${main.google_meet_url?`<p><a class="primary" href="${esc(main.google_meet_url)}" target="_blank" rel="noopener noreferrer">Открыть Google Meet</a></p>`:""}<div class="actions">${actions.includes("approve")?button("approve","Согласовать"):""}${actions.includes("reject")?button("reject","Отклонить","secondary"):""}${actions.includes("approve_reschedule")?button("approve","Согласовать перенос"):""}${actions.includes("reject_reschedule")?button("reject","Отклонить перенос","secondary"):""}${actions.includes("reschedule")?button("move","Перенести","secondary"):""}${actions.includes("cancel")?button("cancel","Отменить встречу","danger"):""}</div>`);
    const prefix=owner()?"admin/":"",resource=m?"meetings":"requests";
    if($("#move"))$("#move").onclick=()=>{state.moving=m;state.day=null;state.time=null;state.view="calendar";closeModal();render();};
    if($("#approve")){const command=key();$("#approve").onclick=e=>mutate(e.target,()=>api(`${prefix}${resource}/${main.id}/${m?"approve-reschedule":"approve"}`,{method:"POST",body:m?{reschedule_request_id:m.pending_reschedule.id}:{},id:command,version:main.version}),"Решение сохранено");}
    for(const action of ["cancel","reject"])if($("#"+action))$("#"+action).onclick=()=>{const command=key();showModal(`<h2 class="modal-title" id="modal-title">${action==="cancel"?"Отменить встречу?":"Отклонить запрос?"}</h2><p class="safe-text">${esc(main.title)}</p><form id="decision"><label class="form-field">Причина${action==="cancel"?" · необязательно":""}<textarea name="reason" maxlength="2000" ${action==="reject"?"required":""}></textarea></label><button class="danger full-width" type="submit">${action==="cancel"?"Да, отменить":"Отклонить"}</button></form>`);
      let last=null,operation=command;$("#decision").onsubmit=e=>{e.preventDefault();const reason=String(new FormData(e.target).get("reason")).trim();if(last!==null&&last!==reason)operation=key();last=reason;const body={reason};if(m&&action==="reject")body.reschedule_request_id=m.pending_reschedule.id;
        mutate(e.submitter||e.target.querySelector("button[type=submit]"),()=>api(`${prefix}${resource}/${main.id}/${action==="reject"&&m?"reject-reschedule":action}`,{method:"POST",body,id:operation,version:main.version}),"Решение сохранено");};};
  }catch(e){errorBox(e);}}
  async function settings(){const data=await api("admin/settings");const fields=[["default_duration","Длительность, минут",15,240],["buffer_minutes","Перерыв, минут",0,120],["daily_limit","Встреч в день",1,50],["booking_horizon_days","Горизонт записи, дней",1,180],["min_booking_notice_hours","Минимум до встречи, часов",1,720]];
    $("#workspace").innerHTML=`<section class="panel settings-panel"><h2>Настройки встреч</h2><form id="settings-form"><div class="settings-grid">${fields.map(([name,label,min,max])=>`<label class="form-field">${label}<input type="number" step="1" min="${min}" max="${max}" name="${name}" value="${data[name]}" required></label>`).join("")}</div><label class="booking-toggle"><span>Принимать новые заявки</span><input type="checkbox" name="booking_enabled" ${data.booking_enabled?"checked":""}></label><p class="muted">Настройки применяются к новым записям. Время существующих встреч сохраняется.</p><p id="form-error" class="inline-error" role="alert"></p><button class="primary" type="submit">Сохранить</button></form></section>`;
    let command=key(),last=null;$("#settings-form").onsubmit=e=>{e.preventDefault();const form=new FormData(e.target),body=Object.fromEntries(fields.map(([name])=>[name,Number(form.get(name))]));body.booking_enabled=form.get("booking_enabled")==="on";const current=JSON.stringify(body);if(last!==null&&last!==current)command=key();last=current;mutate(e.submitter||e.target.querySelector("button[type=submit]"),()=>api("admin/settings",{method:"PATCH",body,id:command,version:data.version}),"Настройки сохранены");};}
  function identity(){const name=state.me.profile.full_name;$("#identity-name").textContent=name;$("#avatar").textContent=name.slice(0,1);$("#account-role").textContent=owner()?"Администратор":"Клиент";}
  $("#close-modal").onclick=closeModal;$("#modal").addEventListener("cancel",e=>{if(state.busy)e.preventDefault();});
  $(".brand").onclick=e=>{e.preventDefault();state.view="calendar";state.moving=null;render();};
  async function start(){tg?.ready();tg?.expand();
    if(!tg?.initData){$("#identity-name").textContent="Meetly";$("#workspace").innerHTML='<section class="panel"><h2>Откройте Meetly в Telegram</h2><p class="muted">Войдите в тестового бота и нажмите кнопку открытия приложения.</p></section>';return;}
    try{const session=await api("session",{method:"POST",auth:"tma "+tg.initData});state.token=session.token;state.me=await api("me");identity();await render();}
    catch(e){$("#identity-name").textContent="Meetly";$("#workspace").innerHTML=`<section class="panel"><h2>Не удалось войти</h2><p class="inline-error" role="alert">${esc(e.message)}</p></section>`;}}
  start();
})();
