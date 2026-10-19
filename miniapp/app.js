/* Interactive design prototype. All records are fictional and stay in this tab. */
"use strict";
const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const dayKey = (date) => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
const todayParts = new Intl.DateTimeFormat("en-CA", {timeZone:"Europe/Moscow", year:"numeric", month:"2-digit", day:"2-digit"}).formatToParts(new Date());
const part = (type) => Number(todayParts.find((p) => p.type === type).value);
const today = new Date(part("year"),part("month")-1,part("day"),12);
const addDays = (date, number) => {const d = new Date(date); d.setDate(d.getDate()+number); return d;};
const horizon = addDays(today,14);
const daysAhead = (number) => dayKey(addDays(today,number));
const dateFromKey = (key) => {const [y,m,d]=key.split("-").map(Number);return new Date(y,m-1,d,12);};
const dateLabel = (key, options={day:"numeric",month:"long"}) => new Intl.DateTimeFormat("ru-RU",options).format(dateFromKey(key));
const statuses = {pending:"Ожидает подтверждения",confirmed:"Подтверждена",reschedule_pending:"Ожидает переноса",cancelled:"Отменена",rejected:"Отклонена"};
const state = {role:"client",view:"calendar",month:new Date(today.getFullYear(),today.getMonth(),1,12),day:null,time:null,filter:"active",reschedule:null,records:[
  {id:1,date:daysAhead(3),time:"11:00",title:"Знакомство и первая консультация",comment:"Хочу обсудить свои цели.",status:"confirmed",client:"Анна",mine:true},
  {id:2,date:daysAhead(6),time:"14:00",title:"Обсуждение дальнейших шагов",comment:"",status:"pending",client:"Анна",mine:true},
  {id:3,date:daysAhead(4),time:"12:00",title:"Индивидуальная консультация",comment:"Удобна онлайн-встреча.",status:"pending",client:"Мария",mine:false},
]};
let toastTimer;
function toast(text){$("#toast").textContent=text;$("#toast").classList.add("visible");clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("#toast").classList.remove("visible"),4000);}
function occupied(date,time){return state.records.some((r)=>r.id!==state.reschedule&&r.date===date&&r.time===time&&["confirmed","pending","reschedule_pending"].includes(r.status));}
function slots(date){const d=dateFromKey(date);if(d<=today||d>horizon||[0,6].includes(d.getDay()))return [];return ["10:00","11:00","12:00","13:00","14:00","15:00","16:00","17:00"].filter((t)=>!occupied(date,t));}
function initialDay(){for(let n=1;n<=14;n++){const key=daysAhead(n);if(slots(key).length)return key;}return null;}
state.day=initialDay();
function navigate(view){state.view=view;state.reschedule=null;state.time=null;render();}
function render(){
  const owner=state.role==="owner";
  $("#identity-name").textContent=owner?"Татьяна · администратор":"Анна";$("#avatar").textContent=owner?"Т":"А";
  $("#navigation").innerHTML=`<button class="nav-button ${state.view==="calendar"?"active":""}" data-nav="calendar"><span class="nav-icon" aria-hidden="true">▦</span>Календарь</button><button class="nav-button ${state.view==="list"?"active":""}" data-nav="list"><span class="nav-icon" aria-hidden="true">☷</span>${owner?"Входящие":"Мои заявки"}</button>`;
  document.querySelectorAll("[data-nav]").forEach((el)=>el.onclick=()=>navigate(el.dataset.nav));
  $("#eyebrow").textContent=owner?"ВСЕ ВСТРЕЧИ ПОД РУКОЙ.":"ВАШЕ ВРЕМЯ. ВАШ РИТМ.";
  $("#page-title").innerHTML=state.reschedule?"Выберите новое<br>время встречи.":state.view==="list"?(owner?"Внимание каждой<br>новой заявке.":"Ваши встречи.<br>Всё на своих местах."):(owner?"Ваш день.<br>В спокойном ритме.":"Найдём время<br>для встречи.");
  $("#page-subtitle").textContent=state.reschedule?"Прежнее время сохранится до согласования.":owner?"Расписание, заявки и решения — в одном месте.":state.view==="list"?"Следите за статусами и управляйте своими записями.":"Выберите удобный день — остальное просто.";
  if(state.view==="calendar")renderCalendar();else renderList();
}
function renderCalendar(){
  const owner=state.role==="owner"&&!state.reschedule;
  const monthLabel=new Intl.DateTimeFormat("ru-RU",{month:"long",year:"numeric"}).format(state.month);
  const first=(state.month.getDay()+6)%7;const count=new Date(state.month.getFullYear(),state.month.getMonth()+1,0).getDate();
  let cells="<span></span>".repeat(first);
  for(let n=1;n<=count;n++){const d=new Date(state.month.getFullYear(),state.month.getMonth(),n,12),key=dayKey(d);const available=owner?state.records.some((r)=>r.date===key):slots(key).length>0;cells+=`<button class="day ${available?"available":""} ${key===state.day?"selected":""} ${key===dayKey(today)?"today":""}" ${!owner&&!available?"disabled":""} data-day="${key}" aria-label="${escapeHTML(dateLabel(key))}${available?owner?", есть записи":", есть свободное время":""}" aria-pressed="${key===state.day}">${n}</button>`;}
  const minMonth=today.getFullYear()*12+today.getMonth(),current=state.month.getFullYear()*12+state.month.getMonth(),maxMonth=horizon.getFullYear()*12+horizon.getMonth();
  $("#workspace").innerHTML=`<div class="workspace-grid"><section class="panel"><div class="panel-heading"><h2 style="text-transform:capitalize">${escapeHTML(monthLabel.replace(" г.",""))}</h2><div class="month-nav"><button id="previous-month" class="icon-button" aria-label="Предыдущий месяц" ${current<=minMonth?"disabled":""}>‹</button><button id="next-month" class="icon-button" aria-label="Следующий месяц" ${current>=maxMonth?"disabled":""}>›</button></div></div><div class="weekdays" aria-hidden="true">${["ПН","ВТ","СР","ЧТ","ПТ","СБ","ВС"].map((d)=>`<span>${d}</span>`).join("")}</div><div class="days">${cells}</div><div class="calendar-legend"><span class="legend-dot" aria-hidden="true"></span>${owner?"В этот день есть встречи или заявки":"Есть свободное время"}</div><div class="note">${owner?"Выберите день, чтобы посмотреть встречи и открыть заявки.":"<strong>Не торопитесь.</strong><br>Выберите время, когда вам будет удобно уделить внимание себе."}</div></section><section class="panel" id="day-panel"></section></div>`;
  $("#previous-month").onclick=()=>changeMonth(-1);$("#next-month").onclick=()=>changeMonth(1);
  document.querySelectorAll("[data-day]").forEach((el)=>el.onclick=()=>{state.day=el.dataset.day;state.time=null;renderCalendar();});
  renderDay();
}
function changeMonth(delta){state.month=new Date(state.month.getFullYear(),state.month.getMonth()+delta,1,12);renderCalendar();}
function meetingHTML(r){return `<article class="meeting-row"><div class="date-tile"><strong>${dateFromKey(r.date).getDate()}</strong>${escapeHTML(dateLabel(r.date,{month:"short"}))}</div><div class="meeting-info"><span class="status ${r.status}">${statuses[r.status]}</span><h3>${escapeHTML(r.title)}</h3><p>${escapeHTML(r.time)}–${endTime(r.time)} · МСК${state.role==="owner"?` · ${escapeHTML(r.client)}`:""}</p></div><button class="row-button" data-record="${r.id}" aria-label="Открыть: ${escapeHTML(r.title)}">↗</button></article>`;}
function endTime(time){return `${String(Number(time.split(":")[0])+1).padStart(2,"0")}:00`;}
function bindRecords(){document.querySelectorAll("[data-record]").forEach((b)=>b.onclick=()=>showRecord(Number(b.dataset.record)));}
function renderDay(){
  const panel=$("#day-panel");if(!state.day){panel.innerHTML='<div class="empty">Свободного времени пока нет.</div>';return;}
  if(state.role==="owner"&&!state.reschedule){const records=state.records.filter((r)=>r.date===state.day);panel.innerHTML=`<p class="tiny-label">РАСПИСАНИЕ ДНЯ</p><h2>${dateLabel(state.day)}</h2><p class="time-label">Московское время · МСК</p>${records.length?records.map(meetingHTML).join(""):'<div class="empty"><div class="empty-symbol" aria-hidden="true">✳</div>На этот день записей нет.</div>'}`;bindRecords();return;}
  const times=slots(state.day);
  panel.innerHTML=`<p class="tiny-label">${state.reschedule?"НОВОЕ ВРЕМЯ":"ВРЕМЯ ДЛЯ ВАС"}</p><h2>${dateLabel(state.day)}</h2><p class="time-label">60 минут · Московское время, МСК</p><div class="slots">${times.map((t)=>`<button class="slot ${state.time===t?"selected":""}" data-time="${t}" aria-pressed="${state.time===t}">${t}</button>`).join("")}</div>${!times.length?'<p class="muted">Свободного времени нет. Выберите другой день.</p>':""}<button id="continue" class="primary full-width" ${!state.time?"disabled":""}>${state.reschedule?"Продолжить перенос":"Записаться на встречу"}<span class="button-arrow" aria-hidden="true">↗</span></button>${state.reschedule?'<button id="stop-reschedule" class="secondary full-width" style="margin-top:10px">Отменить выбор нового времени</button>':'<p class="muted" style="font-size:11px;text-align:center;margin:16px 0 0">После отправки специалист подтвердит встречу.</p>'}<div class="summary-card"><p class="tiny-label">ПРОСТО И ПОНЯТНО</p><p class="muted">Уведомления придут в Telegram и на email.<br>Все ваши записи — в разделе «Мои заявки».</p></div>`;
  document.querySelectorAll("[data-time]").forEach((el)=>el.onclick=()=>{state.time=el.dataset.time;renderDay();});
  $("#continue").onclick=showBookingForm;
  if($("#stop-reschedule"))$("#stop-reschedule").onclick=()=>{state.reschedule=null;render();};
}
function renderList(){
  const owner=state.role==="owner";
  const records=state.records.filter((r)=>owner?["pending","reschedule_pending"].includes(r.status):r.mine&&(state.filter==="all"||!["cancelled","rejected"].includes(r.status))).sort((a,b)=>(a.date+a.time).localeCompare(b.date+b.time));
  $("#workspace").innerHTML=`<section class="panel list-panel"><div class="panel-heading"><h2>${owner?"Ожидают решения":"Мои заявки и встречи"}</h2><span class="muted">${records.length}</span></div>${owner?"":`<div class="filters"><button class="filter ${state.filter==="active"?"active":""}" data-filter="active">Предстоящие</button><button class="filter ${state.filter==="all"?"active":""}" data-filter="all">Все записи</button></div>`}${records.length?records.map(meetingHTML).join(""):'<div class="empty"><div class="empty-symbol" aria-hidden="true">✳</div>Здесь пока нет заявок.<br>Когда они появятся, вы увидите их здесь.</div>'}</section>`;
  document.querySelectorAll("[data-filter]").forEach((el)=>el.onclick=()=>{state.filter=el.dataset.filter;renderList();});bindRecords();
}
function openModal(html){$("#modal-content").innerHTML=html;if(!$("#modal").open)$("#modal").showModal();}
function closeModal(){$("#modal").close();}
function showBookingForm(values={}){
  const moving=Boolean(state.reschedule);
  openModal(`<p class="modal-eyebrow">${moving?"ПЕРЕНОС ВСТРЕЧИ":"НОВАЯ ЗАЯВКА"} · 1 ИЗ 2</p><h2 id="modal-title" class="modal-title">${moving?"Новое удобное время":"О чём поговорим?"}</h2><div class="note" style="margin-bottom:20px">${dateLabel(state.day)} · ${state.time}–${endTime(state.time)} МСК</div><form id="booking-form">${moving?"":`<label class="form-field">Тема встречи<input name="title" required minlength="3" maxlength="100" placeholder="Например, первая консультация" value="${escapeHTML(values.title)}"></label>`}<label class="form-field">Комментарий · необязательно<textarea name="comment" maxlength="2000" placeholder="Что важно знать перед встречей?">${escapeHTML(values.comment)}</textarea></label><button class="primary full-width" type="submit">Проверить ${moving?"перенос":"заявку"} →</button></form>`);
  $("#booking-form").onsubmit=(event)=>{event.preventDefault();const form=new FormData(event.target);const title=moving?state.records.find((r)=>r.id===state.reschedule).title:String(form.get("title")).trim();if(title.length<3){event.target.elements.title.setCustomValidity("Введите минимум 3 символа без пробелов по краям.");event.target.elements.title.reportValidity();event.target.elements.title.oninput=()=>event.target.elements.title.setCustomValidity("");return;}showReview({title,comment:String(form.get("comment")).trim()});};
}
function showReview(values){
  const moving=Boolean(state.reschedule),date=state.day,time=state.time;
  openModal(`<p class="modal-eyebrow">${moving?"ПЕРЕНОС ВСТРЕЧИ":"НОВАЯ ЗАЯВКА"} · 2 ИЗ 2</p><h2 id="modal-title" class="modal-title">Всё верно?</h2><div class="detail-line"><span>Дата</span><strong>${dateLabel(date)}</strong></div><div class="detail-line"><span>Время</span><strong>${time}–${endTime(time)} МСК</strong></div><p class="detail-text">${escapeHTML(values.title)}</p>${values.comment?`<p class="muted detail-text">${escapeHTML(values.comment)}</p>`:""}<div class="note">${moving?"До согласования переноса прежнее время встречи остаётся в силе.":"Заявка поступит специалисту на подтверждение."}<br>В макете действие выполнится только в этой вкладке.</div><div class="actions"><button id="edit-booking" class="secondary">Изменить</button><button id="send-booking" class="primary">Отправить ${moving?"перенос":"заявку"}</button></div>`);
  $("#edit-booking").onclick=()=>showBookingForm(values);
  $("#send-booking").onclick=()=>{if(occupied(date,time)){closeModal();state.time=null;render();toast("Время уже занято. Выберите другое.");return;}if(moving){const r=state.records.find((r)=>r.id===state.reschedule);r.proposed={date,time};r.status="reschedule_pending";r.rescheduleComment=values.comment;}else state.records.push({id:Math.max(...state.records.map((r)=>r.id))+1,date,time,title:values.title,comment:values.comment,status:"pending",client:"Анна",mine:true});closeModal();navigate("list");toast(moving?"Запрос переноса добавлен в макет":"Демонстрационная заявка создана");};
}
function showRecord(id){
  const r=state.records.find((item)=>item.id===id),owner=state.role==="owner",active=["confirmed","reschedule_pending"].includes(r.status);
  openModal(`<p class="modal-eyebrow">${owner?escapeHTML(r.client):"МОЯ ВСТРЕЧА"} · № ${r.id}</p><h2 id="modal-title" class="modal-title">${escapeHTML(r.title)}</h2><span class="status ${r.status}">${statuses[r.status]}</span><div class="detail-line"><span>Дата</span><strong>${dateLabel(r.date)}</strong></div><div class="detail-line"><span>Время</span><strong>${r.time}–${endTime(r.time)} МСК</strong></div>${r.comment?`<p class="muted detail-text">${escapeHTML(r.comment)}</p>`:""}${r.proposed?`<div class="note">Запрошен перенос на <strong>${dateLabel(r.proposed.date)}, ${r.proposed.time} МСК</strong><br>До подтверждения действует прежнее время.${r.rescheduleComment?`<br>${escapeHTML(r.rescheduleComment)}`:""}</div>`:""}${r.reason?`<p class="muted detail-text">Причина: ${escapeHTML(r.reason)}</p>`:""}<div class="actions">${owner&&["pending","reschedule_pending"].includes(r.status)?'<button class="primary" id="approve">Подтвердить</button><button class="secondary" id="reject">Отклонить</button>':""}${!owner&&r.status==="confirmed"?'<button class="secondary" id="move">Перенести</button>':""}${active?'<button class="danger" id="cancel">Отменить встречу</button>':""}</div>`);
  if($("#move"))$("#move").onclick=()=>{state.reschedule=id;state.view="calendar";state.time=null;state.day=initialDay();if(state.day){const d=dateFromKey(state.day);state.month=new Date(d.getFullYear(),d.getMonth(),1,12);}closeModal();render();};
  if($("#cancel"))$("#cancel").onclick=()=>showDecision(r,"cancel");
  if($("#reject"))$("#reject").onclick=()=>showDecision(r,"reject");
  if($("#approve"))$("#approve").onclick=()=>{if(r.proposed){const old=state.reschedule;state.reschedule=r.id;const busy=occupied(r.proposed.date,r.proposed.time);state.reschedule=old;if(busy){toast("Предложенное время уже занято.");return;}r.date=r.proposed.date;r.time=r.proposed.time;delete r.proposed;}r.status="confirmed";closeModal();render();toast("Встреча подтверждена в макете");};
}
function showDecision(record,action){const cancelling=action==="cancel";openModal(`<p class="modal-eyebrow">${cancelling?"ОТМЕНА ВСТРЕЧИ":"ОТКЛОНЕНИЕ ЗАПРОСА"}</p><h2 id="modal-title" class="modal-title">${cancelling?"Отменить встречу?":"Отклонить запрос?"}</h2><p class="muted">${dateLabel(record.date)} · ${record.time} МСК${record.proposed&&!cancelling?". Прежняя встреча сохранится.":""}</p><form id="decision-form"><label class="form-field">Причина${cancelling?" · необязательно":""}<textarea name="reason" maxlength="2000" ${cancelling?"":"required"}></textarea></label><div class="actions"><button class="secondary" id="back-record" type="button">Вернуться</button><button class="danger" type="submit">${cancelling?"Да, отменить":"Отклонить"}</button></div></form>`);$("#back-record").onclick=()=>showRecord(record.id);$("#decision-form").onsubmit=(event)=>{event.preventDefault();record.reason=String(new FormData(event.target).get("reason")).trim();record.status=cancelling?"cancelled":record.proposed?"confirmed":"rejected";delete record.proposed;closeModal();render();toast(cancelling?"Встреча отменена в макете":"Запрос отклонён в макете");};}
$("#close-modal").onclick=closeModal;
$("#role").onchange=(event)=>{state.role=event.target.value;closeModal();navigate("calendar");};
$(".brand").onclick=(event)=>{event.preventDefault();navigate("calendar");};
render();
