'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stages = {S0:'고민·목적 형성',S1:'대안 탐색',S2:'프로젝트 검토',S3:'비교·선택',S4:'참가 가능성 검증',S5:'신청 직전 결정',S6:'신청·결제 진행'};
const labels = {draft_review:'답변 검토 대기',state_selection:'상태 선택 대기',waiting_reply:'고객 반응 대기',pending:'발송 대기',scheduled:'예약됨',staff_check:'직원 확인 대기',awaiting_review:'검토 대기',draft:'초안',ready:'발송 준비',sent:'발송 완료',sending:'전송 확인 중',uncertain:'전송 결과 불명확',failed:'발송 오류',error:'오류',cancelled:'취소',canceled:'취소',blocked:'발송 차단',completed:'완료',done:'완료',consulting:'상담 진행',closed:'상담 종료',paused:'보류',stopped:'중단',active:'후속관리 진행',unknown:'확인 전',incomplete:'미완료 확인',complete:'완료',inbound:'고객 문의',outbound:'발송',phone:'전화 상담',approved:'승인 완료',pending_approval:'승인 대기',revoked:'승인 취소',superseded:'재생성 전 초안',invalidated:'사용 종료',stale:'이전 초안',review:'검토 대기',awaiting_state:'상태 선택 대기'};
let user = null, currentPage = 'dashboard', currentCase = null, templates = [], jobs = [], cases = [], settings = {}, demo = {enabled:false}, toastTimer;
const statePill = s => s ? `<span class="pill teal">${esc(s)} · ${esc(stages[s] || '')}</span>` : '<span class="pill amber">상태 선택 대기</span>';
const statusPill = s => `<span class="pill ${/sent|complete|approved|승인 완료/.test(s || '') ? 'teal' : /error|fail|uncertain|cancel|stopped|closed|revoked|승인 취소/.test(s || '') ? 'red' : /pending|check|review|paused|unknown|state_selection|대기/.test(s || '') ? 'amber' : ''}">${esc(labels[s] || s || '—')}</span>`;
const fmt = v => { if (!v) return '—'; const d = new Date(typeof v === 'number' && v < 1e12 ? v * 1000 : v); return Number.isNaN(d.getTime()) ? esc(v) : new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d); };
const fullFmt = v => { if (!v) return '—'; const d = new Date(typeof v === 'number' && v < 1e12 ? v * 1000 : v); return Number.isNaN(d.getTime()) ? esc(v) : new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',dateStyle:'medium',timeStyle:'short',hour12:false}).format(d); };
const check = v => v ? ' checked' : '';
const selected = (a,b) => a === b ? ' selected' : '';
const svg = path => `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
const icons = {dashboard:svg('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>'),cases:svg('<path d="M4 5h16v14H4zM4 6l8 7 8-7"/>'),jobs:svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/>'),templates:svg('<path d="M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7"/>'),calls:svg('<path d="M6 3l4 5-3 3c2 3 3 4 6 6l3-3 5 4c-1 4-4 4-7 2C8 17 4 13 3 7c0-2 1-4 3-4z"/>'),settings:svg('<path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.5 5.5l2 2m9 9l2 2M5.5 18.5l2-2m9-9l2-2"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>')};
function toast(message,error=false){const el=$('#toast');el.textContent=message;el.className=`show${error?' error':''}`;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.className='',6500);}
async function api(path,method='GET',body) {
  const headers={'Accept':'application/json'};
  if(body!==undefined)headers['Content-Type']='application/json';
  if(user?.csrf_token)headers['X-CSRF-Token']=user.csrf_token;
  const response=await fetch(`/api${path}`,{method,headers,credentials:'same-origin',body:body===undefined?undefined:JSON.stringify(body)});
  const payload=await response.json().catch(()=>({}));
  if(!response.ok){
    const error=new Error(typeof payload.detail==='string'?payload.detail:JSON.stringify(payload.detail||`요청 실패 (${response.status})`));
    error.status=response.status; error.detail=payload.detail;
    if(response.status===401&&path!=='/login') {
      if([...draftBuffers.values()].some(buffer=>buffer.dirty)) {
        error.message='로그인이 만료되어 저장하지 못했습니다. 작성 내용을 복사하여 보관한 뒤 다시 로그인해 주세요.';
      } else {user=null;renderLogin();}
    }
    throw error;
  }
  return payload;
}
function brand(){return '<div class="brand"><div class="brand-mark">g</div><div><strong>한국갭이어</strong><small>COUNSELING WORKSPACE</small></div></div>';}
function renderLogin(){ $('#app').innerHTML=`<div class="login-page"><section class="login-story">${brand()}<div><div class="eyebrow soft-teal">A LITTLE HELP, A BIG CHANGE</div><h1>고객의 다음 한 걸음을<br>함께 만드는 공간.</h1><p>고객의 이야기를 듣고, 필요한 안내를 전하고,<br>다음 연락까지 놓치지 않도록.<br>한국갭이어 상담 워크스페이스입니다.</p><div class="login-art"><span></span><i></i><span></span><i></i><span></span></div></div><footer>한국갭이어 · 직원 전용 상담 관리 시스템</footer></section><section class="login-form-wrap"><form id="login-form" class="login-form"><div class="eyebrow">WELCOME BACK</div><h2>직원 로그인</h2><p class="muted">계정에 로그인하고 오늘의 상담을 시작하세요.</p><label class="field">아이디<input name="username" autocomplete="username" required autofocus></label><label class="field">비밀번호<input name="password" type="password" autocomplete="current-password" required></label><p id="login-error" class="error-text" role="alert"></p><button class="btn primary" type="submit">워크스페이스 입장 →</button><p class="login-note">계정이 없다면 관리자에게 발급을 요청하세요.<br>첫 관리자 계정은 서버의 초기 설정 명령으로 생성합니다.</p></form></section></div>`;}
function shell(){const navs=[['dashboard','대시보드'],['cases','고객 상담'],['jobs','후속관리 현황'],['templates','후속 문구'],['calls','전화 문의']];if(user.role==='admin')navs.push(['settings','관리자 설정']);$('#app').innerHTML=`<div class="shell"><aside class="sidebar">${brand()}<div class="nav-label">WORKSPACE</div><nav class="nav" aria-label="주 메뉴">${navs.map(([key,title])=>`<button data-nav="${key}" class="${currentPage===key?'active':''}">${icons[key]}${title}</button>`).join('')}</nav><div class="sidebar-bottom"><div class="sidebar-note">직원이 판단하고,<br>자동화가 다음 연락을 돕습니다.<br><br>기본 시간대 · 서울</div></div></aside><div class="main-wrap"><header class="topbar"><div class="breadcrumb">상담 워크스페이스 <span aria-hidden="true"> / </span> <span id="breadcrumb-page"></span></div><div class="top-actions"><span class="pill ${settings.mode==='live'?'amber':'teal'}"><span class="dot"></span>${settings.mode==='live'?'실제 연동 모드':'모의 운영 모드'}</span><div class="user"><div class="avatar">${esc(user.username.slice(0,1).toUpperCase())}</div><div class="user-name">${esc(user.username)}<small>${user.role==='admin'?'관리자':'일반 직원'}</small></div></div><button class="btn ghost small" data-action="logout">로그아웃</button></div></header><main id="main" class="main"></main></div></div>`;}
function pageHeading(title,subtitle,actions='',eyebrow='COUNSELING WORKSPACE'){return demoPanel()+`<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${subtitle}</p></div><div class="actions">${actions}</div></div>`;}
async function refreshDemo(caseId){demo=await api(`/demo${caseId?`?case_id=${encodeURIComponent(caseId)}`:''}`);return demo;}
function demoPanel(){
 if(!demo.enabled)return '';
 const caseId=demo.case_id, base=demo.base_sent_at, days=demo.days||[1,3,7,14,30];
 const ready=!!base;
 return `<section class="demo-panel" aria-label="직접 검수 도구"><div class="demo-panel-head"><div><span class="eyebrow">STAFF TEST DRIVE</span><h2>직접 검수 · 실제 발송 차단</h2><p>모의 Gmail과 <strong>모의 응답</strong>을 사용합니다. 실제 GPT가 생성한 답변이 아닙니다.</p></div><div class="demo-clock"><span>테스트 시각 · 서울</span><strong>${fullFmt(demo.now)}</strong><small>기준 답장 발송: ${base?fullFmt(base):'아직 발송하지 않음'}</small></div></div><div class="demo-guide"><p><b>① 무응답 검수</b> 문의 확인 → S4 선택 → 모의 응답 수정·승인 → 모의 발송 후 아래 버튼을 순서대로 누르세요.</p><p><b>② 고객 답장 검수</b> 첫 후속 연락 후 고객 답장을 접수하면 남은 예약이 취소됩니다. 새 검수 문의를 만들면 기존 기록을 유지하면서 별도로 반복할 수 있습니다.</p></div><div class="demo-schedule">${days.map((day,i)=>{const passed=base&&Date.parse(base)+Number(day)*86400000<=Date.parse(demo.now);return `<button class="demo-day" data-action="demo-advance" data-day="${Number(day)}" data-id="${esc(caseId||'')}"${!ready||passed?' disabled':''}><strong>기준 답장 +${Number(day)}일</strong><span>S4-${String(i+1).padStart(2,'0')}</span><small>${passed?'이미 지난 시점':'시간 이동 · 예약 실행'}</small></button>`;}).join('')}</div><p class="demo-footnote">1·3·7·14·30일은 누적 일정입니다. 주말·영업시간 밖이면 다음 발송 가능 시간으로 이동합니다. 테스트 데이터에서 승인된 문구: ${esc((demo.approved_templates||[]).join(', ')||'없음')}.</p><div class="actions">${caseId?`<button class="btn primary small" data-nav="case/${esc(caseId)}">검수 문의 열기</button><button class="btn small" data-action="demo-reply" data-id="${esc(caseId)}">고객 답장 접수 · 예약 취소 확인</button>`:''}<button class="btn small" data-action="demo-scenario">＋ 새 검수 문의</button></div></section>`;
}
async function showDemoCase(caseId){if(location.hash===`#case/${caseId}`)await renderCase(caseId);else location.hash=`case/${caseId}`;}
function demoReplyModal(caseId){openModal('테스트 고객 답장 접수',`<div class="notice">가상 고객의 새 답장을 기록하고 이 고객의 남은 후속 예약을 취소합니다.</div><form id="demo-reply-form" data-id="${esc(caseId)}"><label class="field">고객 답장<textarea name="body" rows="4" required>1월 출발 일정을 다시 확인하고 싶습니다. 3주 기준으로 필요한 조건을 알려주세요.</textarea></label><button class="btn primary" type="submit">모의 답장 접수</button></form>`);}
function empty(title,body,action=''){return `<div class="empty"><div class="empty-symbol" aria-hidden="true">○</div><h3>${title}</h3><p>${body}</p>${action}</div>`;}
function caseRows(rows){return rows.map(c=>`<tr><td><button class="row-link" data-nav="case/${esc(c.id)}"><strong>${esc(c.name||'이름 미등록')}</strong><small>${esc(c.email||c.phone||'연락처 미등록')}</small></button></td><td><button class="row-link" data-nav="case/${esc(c.id)}">${esc(c.subject||'전화 상담')}</button><br><small>${c.channel==='phone'?'전화':'이메일'} · ${esc(c.owner||'담당 미지정')}</small></td><td>${statePill(c.state)}</td><td>${statusPill(c.workflow)}</td><td>${fmt(c.next_due||c.updated_at)}</td><td><button class="btn small" data-nav="case/${esc(c.id)}">상담 열기 ↗</button></td></tr>`).join('');}
function caseTable(rows){return rows.length?`<div class="table-wrap"><table><thead><tr><th>고객</th><th>상담 내용</th><th>고객 상태</th><th>업무 상태</th><th>다음 연락 / 최근 활동</th><th></th></tr></thead><tbody>${caseRows(rows)}</tbody></table></div>`:empty('등록된 상담이 없습니다','Gmail 문의를 확인하거나 전화 상담을 등록하면 이곳에서 관리할 수 있습니다.',`<button class="btn" data-nav="calls">전화 문의 등록</button>`);}
function filterCases(){const q=$('#case-search').value.toLowerCase(),s=$('#case-filter').value;const rows=cases.filter(c=>(!s||c.state===s)&&`${c.name} ${c.email} ${c.subject} ${c.phone}`.toLowerCase().includes(q));$('#case-table').innerHTML=caseTable(rows);$('#case-count').textContent=`총 ${rows.length}건`;}
function historyHTML(history){return history?.length?`<div class="timeline">${history.map(m=>`<article class="message ${m.direction==='outbound'?'outbound':''}"><div class="message-head"><strong>${esc(m.direction==='outbound'?(m.source==='followup'?'예약 후속 연락':'직원 답변'):m.source==='phone'?'전화 상담':'고객 문의')}</strong><span class="muted">${fmt(m.received_at||m.created_at)}</span></div>${m.subject?`<p class="muted">${esc(m.subject)}</p>`:''}<div class="message-body">${esc(m.body)}</div></article>`).join('')}</div>`:empty('아직 상담 기록이 없습니다','문의가 접수되면 상담 내용이 표시됩니다.');}
function jobTable(rows,compact=false){if(!rows.length)return empty('등록된 후속 일정이 없습니다','직원이 승인한 답변이 정상 발송되면 적용 가능한 후속 일정이 생성됩니다.');return `<div class="table-wrap"><table><thead><tr>${compact?'':'<th>고객 / 상태</th>'}<th>문구 / 단계</th><th>예정일</th><th>처리 상태</th><th>대기·취소 사유</th><th></th></tr></thead><tbody>${rows.map(j=>`<tr>${compact?'':`<td><button class="row-link" data-nav="case/${esc(j.case_id)}"><strong>${esc(j.name||j.customer_name||'상담 고객')}</strong></button>${statePill(j.state)}</td>`}<td><strong>${esc(j.template_id)}</strong><small>버전 ${esc(j.template_version||1)}</small></td><td>${fmt(j.due_at)}<br><small>기준 ${fmt(j.original_due_at)}</small></td><td>${statusPill(j.status)}</td><td class="max-text">${esc(j.reason||'—')}${j.cancelled_at?`<br><small>${fmt(j.cancelled_at)}</small>`:''}</td><td>${!['sent','cancelled','canceled','uncertain','sending'].includes(j.status)?`<button class="btn small" data-action="review-job" data-id="${esc(j.id)}">문구 확인${String(j.template_id).startsWith('S6')||j.activation_review_required?'·승인':''}</button>`:''}</td></tr>`).join('')}</tbody></table></div>`;}
async function renderJobs(){jobs=await api('/jobs');$('#main').innerHTML=pageHeading('후속관리 현황','예약부터 발송, 취소 사유까지 모든 단계를 확인합니다.',`<button class="btn" data-action="refresh">↻ 새로고침</button>`)+`<div class="notice">S0–S5는 승인된 문구와 연락 조건을 확인한 뒤 실행합니다. S6는 신청·결제 상태 확인과 직원의 개별 승인이 필요합니다.</div><div class="toolbar"><select id="job-filter" aria-label="후속 상태 필터"><option value="">모든 처리 상태</option><option value="pending">발송 대기</option><option value="staff_check">직원 확인 대기</option><option value="sent">발송 완료</option><option value="cancelled">취소</option><option value="uncertain">결과 불명확</option></select><span class="selection-count">총 ${jobs.length}건</span></div><section class="card" id="jobs-table">${jobTable(jobs)}</section>`;}
function templateCards(rows){return rows.length?`<div class="template-grid">${rows.map(t=>`<article class="card template-card"><div class="card-header"><div class="template-title"><strong>${esc(t.id)}</strong><span>${esc(t.role)}</span></div>${statusPill(t.approval)}</div><div class="card-body"><div class="actions mb15"><span class="pill">${esc(t.stage||stages[t.state])}</span><span class="pill">${esc(t.days)}일 후</span><span class="pill ${t.state==='S6'?'amber':''}">${esc(t.automation)}</span></div><p class="body-text">${esc(t.body)}</p></div><footer><small>버전 ${esc(t.version)} · ${esc(t.channel||'이메일')}</small><button class="btn small" data-action="edit-template" data-id="${esc(t.id)}">${user.role==='admin'?'문구 검토 · 관리':'문구 상세'}</button></footer></article>`).join('')}</div>`:empty('검색 결과가 없습니다','문구 ID 또는 상태 필터를 변경해 주세요.');}
async function renderTemplates(){templates=await api('/templates');$('#main').innerHTML=pageHeading('후속 문구 관리','원본 Excel의 S0–S6 문구를 검토하고 승인 상태를 관리합니다.',`<span class="pill teal">${templates.length}개 문구</span>`)+`<div class="notice amber">문구 수정 시 새 버전은 승인 대기로 등록됩니다. 미승인 문구는 발송되지 않으며, 기존 예약의 문구를 임의로 교체하지 않습니다.</div><div class="toolbar"><input type="search" id="template-search" class="search" placeholder="문구 ID, 내용 검색" aria-label="문구 검색"><select id="template-filter" aria-label="문구 상태 필터"><option value="">S0–S6 전체</option>${Object.keys(stages).map(s=>`<option>${s}</option>`).join('')}</select><select id="approval-filter" aria-label="승인 상태 필터"><option value="">모든 승인 상태</option><option value="pending">승인 대기</option><option value="approved">승인 완료</option></select></div><div id="template-grid">${templateCards(templates)}</div>`;}
function filterTemplates(){const q=$('#template-search').value.toLowerCase(),s=$('#template-filter').value,a=$('#approval-filter').value;const rows=templates.filter(t=>(!s||t.state===s)&&(!a||(a==='approved'?['approved','승인 완료'].includes(t.approval):!['approved','승인 완료'].includes(t.approval)))&&`${t.id} ${t.body}`.toLowerCase().includes(q));$('#template-grid').innerHTML=templateCards(rows);}
async function renderCalls(){const rows=await api('/calls');$('#main').innerHTML=pageHeading('전화 문의 관리','상담 내용을 기록하고 필요한 다음 전화를 예약합니다.',`<button class="btn primary" data-action="new-phone">＋ 전화 문의 등록</button>`)+`<div class="notice">전화 상담만으로 이메일 후속관리가 시작되지 않습니다. 이메일이 없는 고객은 전화 업무로 관리합니다.</div><section class="card"><div class="card-header"><h2>전화 연락 업무</h2><small>${rows.length}건</small></div>${rows.length?`<div class="table-wrap"><table><thead><tr><th>고객</th><th>전화번호</th><th>전화 예정일</th><th>상태</th><th></th></tr></thead><tbody>${rows.map(c=>`<tr><td><button class="row-link" data-nav="case/${esc(c.case_id)}"><strong>${esc(c.name)}</strong></button></td><td>${esc(c.phone||'—')}</td><td>${fullFmt(c.due_at)}</td><td>${statusPill(c.status)}</td><td><div class="actions"><button class="btn small" data-nav="case/${esc(c.case_id)}">상담 열기</button>${!['done','completed','cancelled'].includes(c.status)?`<button class="btn small" data-action="complete-call" data-id="${esc(c.id)}">전화 완료</button>`:''}</div></td></tr>`).join('')}</tbody></table></div>`:empty('예정된 전화 연락이 없습니다','전화 문의를 등록하고 후속 전화 예정일을 지정해 주세요.')}</section>`;}
function connectionText(s){if(!s||!Object.keys(s).length)return '아직 외부 서비스 연결 검증을 실행하지 않았습니다.';return Object.entries(s).map(([k,v])=>`${({gmail:'Gmail',openai:'OpenAI',sheets:'Google Sheets'})[k]||k}: ${typeof v==='object'?Object.entries(v).map(([a,b])=>`${a}: ${typeof b==='object'?JSON.stringify(b):b}`).join(' · '):v}`).join('\n');}
function openModal(title,body,footer=''){const dialog=$('#modal');$('#modal-content').innerHTML=`<div class="modal-head"><h2>${title}</h2><button class="btn ghost small" data-action="close-modal" aria-label="닫기">✕</button></div><div class="modal-body">${body}</div>${footer?`<div class="modal-foot">${footer}</div>`:''}`;if(!dialog.open)dialog.showModal();}
function closeModal(){$('#modal').close();}
function phoneModal(){openModal('전화 문의 등록',`<form id="phone-form"><div class="form-grid"><label class="field">고객 이름<input name="name" required></label><label class="field">전화번호<input name="phone" type="tel" required></label><label class="field full">이메일 (선택)<input name="email" type="email"></label><label class="field">상담 일시 · 서울<input name="consulted_at" type="datetime-local" value="${seoulLocalNow()}" required></label><label class="field">담당 직원<input name="owner" value="${esc(user.username)}" required></label><label class="field full">상담 내용<textarea name="body" rows="4" required></textarea></label><label class="field">고객 상태<select name="state" required><option value="">상태를 선택하세요</option>${Object.entries(stages).map(([s,n])=>`<option value="${s}">${s} · ${n}</option>`).join('')}</select></label><label class="field">후속 전화 예정일 · 서울<input name="followup_at" type="datetime-local"></label></div><label class="checkbox"><input name="email_allowed" type="checkbox">이메일 후속 연락 가능 여부를 확인했습니다.</label><label class="field">연락 허용 근거<input name="consent_note"></label><div class="actions"><button class="btn primary" type="submit">상담 등록</button><button class="btn" type="button" data-action="close-modal">취소</button></div></form>`);}
function seoulLocalNow(){return new Date((demo.enabled?Date.parse(demo.now):Date.now())+9*60*60*1000).toISOString().slice(0,16);}
function utcFromSeoul(v){return v?new Date(`${v}:00+09:00`).toISOString():null;}
function mockModal(reply=false){const c=reply?currentCase:null;openModal(reply?'모의 고객 답장':'모의 Gmail 문의 접수',`<div class="notice">개발 검증용 모의 메일입니다. 실제 메일을 수신하거나 발송하지 않습니다.</div><form id="mock-form"><label class="field">고객 이름<input name="name" value="${esc(c?.name||'테스트 고객')}" required></label><label class="field">고객 이메일<input name="email" type="email" value="${esc(c?.email||'customer@example.test')}" required></label><label class="field">제목<input name="subject" value="${esc(c?.subject||'갭이어 프로젝트 문의')}" required></label><label class="field">문의 내용<textarea name="body" rows="5" required></textarea></label>${c?.thread_id?`<input type="hidden" name="thread_id" value="${esc(c.thread_id)}">`:''}<button class="btn primary" type="submit">${reply?'고객 답장 접수':'문의 접수'}</button></form>`);}
function templateModal(id){const t=templates.find(t=>t.id===id);if(!t)return;const admin=user.role==='admin';openModal(`${esc(t.id)} · 문구 ${admin?'관리':'상세'}`,`<div class="actions">${statePill(t.state)}${statusPill(t.approval)}<span class="pill">버전 ${esc(t.version)}</span></div><dl class="definition"><dt>발송 시점</dt><dd>첫 답장 실제 발송 후 ${esc(t.days)}일</dd><dt>자동화 분류</dt><dd>${esc(t.automation)}</dd><dt>필수 조건</dt><dd>${esc(t.conditions)}</dd><dt>참고</dt><dd>${esc(t.note||'—')}</dd></dl><form id="template-form" data-id="${esc(t.id)}"><label class="field">문구 전문<textarea name="body" rows="7"${admin?'':' readonly'} required>${esc(t.body)}</textarea></label>${admin?`<div class="actions"><button class="btn" type="submit">수정 · 새 버전 저장</button><button class="btn primary" type="button" data-action="approve-template" data-id="${esc(t.id)}">현재 저장본 승인</button><button class="btn danger" type="button" data-action="revoke-template" data-id="${esc(t.id)}">승인 취소</button></div><small>승인은 저장된 문구에 적용됩니다. 수정한 내용은 먼저 저장하세요.</small>`:''}</form><hr class="divider"><details><summary>문구 변경 이력 (${(t.history||[]).length})</summary>${(t.history||[]).map(v=>`<div class="version-block"><strong>버전 ${esc(v.version)}</strong> ${statusPill(v.approval)}<p class="muted">${esc(v.actor||v.created_by||'')} · ${fullFmt(v.created_at)}</p><pre>${esc(v.body)}</pre></div>`).join('')}</details>`);}
async function reviewJob(id) {
  const selectedJob=jobs.find(job=>String(job.id)===String(id));
  if(!selectedJob)return;
  const [ts,c,operations]=await Promise.all([api('/templates'),api(`/cases/${encodeURIComponent(selectedJob.case_id||currentCase.id)}`),api('/operations')]);
  const job=c.jobs.find(item=>String(item.id)===String(id));
  if(!job)throw new Error('예약을 찾을 수 없습니다. 목록을 새로고침해 주세요.');
  const template=ts.find(item=>item.id===job.template_id);
  const version=template?.history?.find(item=>Number(item.version)===Number(job.template_version))||(Number(template?.version)===Number(job.template_version)?template:null);
  if(!version)throw new Error('예약에 지정된 문구 버전을 확인할 수 없습니다. 발송을 보류하고 관리자에게 확인해 주세요.');
  let body=version.body||'';
  for(const[key,value]of Object.entries(c.fields||{}))if(value)body=body.replaceAll(`[${key}]`,String(value));
  const isS6=String(job.template_id).startsWith('S6');
  const held=!!job.activation_review_required;
  const reviewRequired=isS6||held;
  const terminal=['sent','cancelled','canceled','uncertain','sending'].includes(job.status);
  openModal(`${esc(job.template_id)} · ${reviewRequired?'직원 개별 확인·승인':'예약 문구 확인'}`,
    `<div class="actions">${statusPill(job.status)}<span class="pill">문구 버전 ${Number(job.template_version)}</span></div>
    <dl class="definition"><dt>보내는 계정</dt><dd>${settings.mode==='live'?esc(operations.sender||'연결 확인 필요'):'모의 Gmail'}</dd><dt>받는 사람</dt><dd>${esc(c.name||'')} · ${esc(c.email||'이메일 없음')}</dd><dt>상담 제목</dt><dd>${esc(c.subject||'')}</dd><dt>예정 시각</dt><dd>${fullFmt(job.due_at)}</dd><dt>기준 일정</dt><dd>${fullFmt(job.original_due_at)}</dd><dt>보류 사유</dt><dd>${esc(job.reason||'발송 조건을 확인합니다.')}</dd></dl>
    <div class="message"><div class="message-body">${esc(body)}</div></div>
    ${held?'<div class="notice amber section-gap">중지 기간 또는 운영 활성화 전에 시점이 지난 예약입니다. 자동으로 재개하지 않습니다. 이 예약 한 건의 필요성과 현재 고객 상황을 확인한 뒤 승인하세요.</div>':''}
    ${reviewRequired&&!terminal?`<form id="job-form" data-id="${esc(job.id)}" data-case="${esc(c.id)}" data-s6="${isS6?'true':'false'}">${isS6?'<div class="notice amber section-gap">S6은 최신 신청·결제 미완료 확인과 건별 승인이 모두 필요합니다.</div><label class="field">지금 확인한 신청·결제 상태<select name="payment" required><option value="">선택하세요</option><option value="incomplete">미완료 — 안내가 필요합니다</option><option value="complete">완료 — 후속 연락을 중단합니다</option><option value="stopped">중단 — 후속 연락을 중단합니다</option></select></label>':''}<label class="checkbox"><input type="checkbox" name="reviewed" required>수신자, 예약 시점과 보류 사유, 위 문구를 검토했습니다. 발송 조건 충족 시 이 한 건의 ${settings.mode==='live'?'실제':'모의'} 발송을 승인합니다.</label><button class="btn primary" type="submit">${isS6?'확인 결과 적용 · 미완료 시 발송 승인':'이 예약 한 건 승인·발송'}</button></form>`:`<p class="muted text-mt15">${terminal?'이미 처리 중이거나 완료된 예약입니다. 추가 발송하지 않습니다.':'발송 조건을 모두 충족하면 예약 실행기가 처리합니다. 부족한 정보는 고객 상세에서 수정하세요.'}</p><div class="actions section-gap"><button class="btn" data-action="close-modal">닫기</button></div>`}`);
}
async function runButton(button,fn){if(button.disabled)return;button.disabled=true;try{await fn();}catch(e){toast(e.message,true);}finally{if(button.isConnected)button.disabled=false;}}

// Draft buffers stay in memory while the server is the durable source of truth.
const draftBuffers = new Map();
let activeRoute = '', operationStatus = {};
const queueTitles = {new_inquiries:'신규 문의',state_selection:'상태 선택 대기',draft_review:'답변 검토 대기',new_replies:'고객 답장 도착',today_followups:'오늘 후속 예정',sent_followups:'후속 발송 완료',staff_check:'직원 확인 필요',errors:'발송 오류 · 중단'};
const activationLabels = {verification_gmail_received:'확인용 수신 주소에서 보낸 실제 Gmail 문의 수신',verification_openai_generated:'확인용 문의의 실제 OpenAI 답변 생성',verification_staff_sent:'직원 승인 후 확인용 수신자에게 실제 답장 발송',verification_reply_received:'확인용 고객의 실제 답장 수신 및 남은 예약 취소',verification_followup_sent:'확인용 수신자에게 승인 후속 문구 1건 발송',verification_reply_cancelled:'고객 답장으로 남은 확인용 예약 취소'};

function bufferFor(draft, caseId) {
  let buffer = draftBuffers.get(draft.id);
  if (!buffer) {
    buffer = {id:draft.id,caseId,body:draft.body,savedBody:draft.body,version:draft.edit_version,dirty:false,status:'saved',timer:null,pending:null,error:'',conflict:false,stale:draft.status!=='draft'};
    draftBuffers.set(draft.id, buffer);
  } else if (!buffer.dirty && !buffer.pending) {
    Object.assign(buffer,{body:draft.body,savedBody:draft.body,version:draft.edit_version,status:'saved',error:'',conflict:false});
  }
  buffer.stale = draft.status !== 'draft';
  if (buffer.stale) clearTimeout(buffer.timer);
  return buffer;
}

function updateSaveStatus(buffer) {
  const form = $('#draft-form');
  if (!form || form.dataset.id !== buffer.id) return;
  form.dataset.version = String(buffer.version);
  const status = $('#draft-save-status');
  const messages = {saving:'저장 중…',saved:'저장 완료',unsaved:'변경 내용 저장 대기',failed:'저장 실패 · 작성 내용은 이 화면에 보존됨',conflict:'다른 직원의 수정과 충돌 · 비교 후 저장 필요',stale:'새 문의 또는 상태 변경으로 이전 초안이 됨'};
  status.textContent = buffer.stale ? messages.stale : messages[buffer.status] || messages.unsaved;
  status.className = `draft-save-status ${buffer.status}`;
  $('#draft-save-error').textContent = buffer.error || '';
  const conflictButton = $('[data-action=resolve-draft]');
  if (conflictButton) conflictButton.hidden = !buffer.conflict;
  const sendButton = $('[data-action=review-send]');
  if (sendButton) sendButton.disabled = buffer.stale || buffer.conflict || !currentCase.email || !!currentCase.operational_status?.manual_send_blocked;
}

async function saveDraftBuffer(buffer, force=false) {
  clearTimeout(buffer.timer);
  if (buffer.pending) {
    await buffer.pending;
    if (buffer.dirty) return saveDraftBuffer(buffer,force);
    return;
  }
  if (!buffer.dirty) return;
  if (buffer.stale) throw new Error('이전 문의의 초안은 발송할 수 없습니다. 보존된 내용을 확인하고 최신 문의의 새 답변을 작성해 주세요.');
  if (buffer.conflict && !force) throw new Error('다른 직원이 저장한 최신 내용과 비교한 뒤 저장해 주세요.');
  const body = buffer.body;
  buffer.status = 'saving'; buffer.error = ''; updateSaveStatus(buffer);
  buffer.pending = (async () => {
    try {
      const saved = await api(`/drafts/${encodeURIComponent(buffer.id)}`,'PUT',{body,expected_version:buffer.version});
      buffer.version = saved.edit_version;
      buffer.savedBody = body;
      buffer.dirty = buffer.body !== body;
      buffer.status = buffer.dirty ? 'unsaved' : 'saved';
      buffer.conflict = false;
      const draft = currentCase?.drafts?.find(d => d.id === buffer.id);
      if (draft) Object.assign(draft,saved);
    } catch (error) {
      buffer.status = error.status === 409 ? 'conflict' : 'failed';
      buffer.conflict = error.status === 409;
      buffer.error = error.message;
      throw error;
    } finally {
      buffer.pending = null;
      updateSaveStatus(buffer);
    }
  })();
  await buffer.pending;
  if (buffer.dirty) return saveDraftBuffer(buffer,force);
}

function editDraftBody(form) {
  const buffer = draftBuffers.get(form.dataset.id);
  if (!buffer) return;
  buffer.body = form.elements.body.value;
  buffer.dirty = buffer.body !== buffer.savedBody;
  if (!buffer.conflict) buffer.status = buffer.dirty ? 'unsaved' : 'saved';
  updateSaveStatus(buffer);
  clearTimeout(buffer.timer);
  if (buffer.dirty && !buffer.conflict && !buffer.stale) {
    buffer.timer = setTimeout(() => saveDraftBuffer(buffer).catch(() => {}),750);
  }
}

async function guardDraftNavigation() {
  const pending = [...draftBuffers.values()].filter(b => b.dirty && b.caseId === currentCase?.id);
  if (!pending.length) return true;
  if (!window.confirm('아직 저장되지 않은 답변이 있습니다. 먼저 저장한 뒤 이동할까요? 취소하면 현재 화면을 유지합니다.')) return false;
  try {
    for (const buffer of pending) await saveDraftBuffer(buffer);
    return true;
  } catch (error) {
    toast(`${error.message} 작성 내용을 보호하기 위해 현재 화면을 유지합니다.`,true);
    return false;
  }
}

async function resolveDraftConflict() {
  const buffer = draftBuffers.get($('#draft-form')?.dataset.id);
  if (!buffer) return;
  const fresh = await api(`/cases/${encodeURIComponent(buffer.caseId)}`);
  const server = fresh.drafts.find(d => d.id === buffer.id);
  if (!server || server.status !== 'draft' || fresh.generation !== currentCase.generation) {
    buffer.stale = true;
    updateSaveStatus(buffer);
    openModal('이전 초안 보존',`<div class="notice amber">새 문의나 상태 변경으로 이전 초안이 되었습니다. 아래 작성 내용은 복사할 수 있습니다. 최신 문의를 확인하고 새 답변을 작성해 주세요.</div><textarea rows="12" readonly>${esc(buffer.body)}</textarea><button class="btn" data-action="discard-local-draft" data-id="${esc(buffer.id)}">작성 내용 보관 후 최신 문의 확인</button>`);
    return;
  }
  openModal('다른 직원의 수정과 비교',`<div class="notice amber">자동으로 덮어쓰지 않았습니다. 서버의 최신 내용과 내 작성 내용을 비교한 뒤 최종 내용을 직접 확인해 주세요.</div><details open><summary>서버에 저장된 최신 답변 · 버전 ${Number(server.edit_version)}</summary><div class="message-body version-block">${esc(server.body)}</div></details><form id="draft-merge-form" data-id="${esc(buffer.id)}" data-version="${Number(server.edit_version)}"><label class="field section-gap">내 작성 내용 · 필요한 내용을 합쳐 최종본으로 수정<textarea name="body" rows="12" required>${esc(buffer.body)}</textarea></label><label class="checkbox"><input type="checkbox" required>서버 최신 내용과 비교했으며 아래 저장으로 반영할 내용을 확인했습니다.</label><button class="btn primary" type="submit">확인한 내용 저장</button></form>`);
}

function draftHTML(draft,c) {
  if (!draft) return empty('고객에게 맞는 답변을 준비하세요','최신 문의를 읽고 고객 상태를 저장한 뒤 답변을 생성해 주세요.');
  const b = bufferFor(draft,c.id), meta = draft.generation_metadata || {};
  return `<form id="draft-form" data-id="${esc(draft.id)}" data-version="${Number(b.version)}">
    <div class="actions mb15">${statusPill(draft.status)}<span class="pill">${draft.provider==='mock'?'모의 응답 · 실제 GPT 생성 아님':'OpenAI 생성 초안'}</span></div>
    ${draft.provider!=='mock'?`<details class="generation-details"><summary>생성 정보</summary><p class="muted small-mb20">모델 ${esc(meta.model||draft.provider)} · ${fullFmt(meta.generated_at||draft.created_at)}${meta.request_id?` · 요청 ${esc(meta.request_id)}`:''}${meta.usage?.total_tokens?` · ${Number(meta.usage.total_tokens)} 토큰`:''}</p></details>`:''}
    ${draft.checks?.length?`<div class="notice amber"><strong>직원 확인 사항 · 고객 발송 본문에 포함되지 않음</strong><ul>${draft.checks.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:''}
    <label class="field">고객에게 보낼 답변<textarea name="body" rows="12" required>${esc(b.body)}</textarea></label>
    <div class="save-feedback"><span id="draft-save-status" role="status" aria-live="polite"></span><span id="draft-save-error" class="error-text"></span></div>
    <div class="actions"><button class="btn" type="submit">지금 저장</button><button class="btn" type="button" data-action="resolve-draft" hidden>충돌 내용 비교</button><button class="btn primary" type="button" data-action="review-send" data-id="${esc(draft.id)}"${!c.email||c.operational_status?.manual_send_blocked?' disabled':''}>${demo.enabled?'검토 후 모의 발송':'검토 후 발송'}</button></div>
    ${!c.email?'<p class="inline-feedback">이메일이 없는 고객입니다. 작성한 문구를 전화 상담에 사용해 주세요.</p>':''}
  </form>`;
}

function archivedDraftsHTML(drafts) {
  const archived = drafts.filter(d=>d.status!=='draft' && d.status!=='sent');
  if (!archived.length) return '';
  return `<details class="archived-drafts section-gap"><summary>보존된 이전 초안 ${archived.length}개 · 직접 발송되지 않음</summary>${archived.map(d=>{
    const buffer=draftBuffers.get(d.id);
    if (buffer) buffer.stale=true;
    return `<div class="version-block"><div class="actions">${statusPill(d.status)}<small>${fullFmt(d.created_at)}</small></div><div class="message-body">${esc(d.body)}</div>${buffer?.dirty?`<label class="field section-gap">서버에 저장되지 못한 작성 내용 · 복사하여 보관 가능<textarea rows="8" readonly>${esc(buffer.body)}</textarea></label><button class="btn small" data-action="discard-local-draft" data-id="${esc(buffer.id)}">내용 보관 후 미저장 표시 해제</button>`:''}</div>`;
  }).join('')}</details>`;
}

function latestInquiryHTML(c) {
  const latest = [...(c.history||[])].filter(m=>m.direction==='inbound').sort((a,b)=>Date.parse(b.received_at||b.created_at)-Date.parse(a.received_at||a.created_at))[0];
  return `<section class="card latest-inquiry" id="latest-inquiry"><div class="card-header"><h2>최신 고객 문의</h2><span class="pill">${latest?.source==='phone'?'전화 상담':'이메일 문의'}</span></div><div class="card-body">${latest?`<dl class="definition"><dt>발신자</dt><dd>${esc(c.name||'')} ${esc(latest.sender||latest.email||c.email||c.phone||'')}</dd><dt>제목</dt><dd>${esc(latest.subject||c.subject||'전화 문의')}</dd><dt>수신 시각</dt><dd>${fullFmt(latest.received_at||latest.created_at)}</dd></dl><div class="message-body">${esc(latest.body)}</div>`:empty('문의 내용을 확인해 주세요','상담이력에 기록된 내용을 확인한 뒤 상태를 선택해 주세요.')}</div></section>`;
}

function requiredFollowupFields(c) {
  const pending=(c.jobs||[]).filter(job=>['pending','staff_check'].includes(job.status));
  const sources=pending.length?pending.map(job=>{
    const template=templates.find(item=>item.id===job.template_id);
    return template?.history?.find(item=>Number(item.version)===Number(job.template_version))||template;
  }).filter(Boolean):templates.filter(item=>item.state===c.state&&['approved','승인 완료'].includes(item.approval));
  return [...new Set(sources.flatMap(item=>[...(String(item.body||'')+' '+String(item.conditions||'')).matchAll(/\[([^\]]+)\]/g)].map(match=>match[1])))];
}

function caseInfoHTML(c) {
  const cu=c.customer||c, fields=c.fields||{};
  const requiredFields=requiredFollowupFields(c);
  const missing=requiredFields.filter(key=>!String(fields[key]||'').trim());
  const allFields=[...new Set([...Object.keys(fields),...requiredFields,'프로젝트명','A 프로젝트','B 프로젝트','핵심 조건','일정','기간','예산'])];
  const paymentField=`<label class="field">신청·결제 확인 결과<select name="payment">${[['unknown','상태 확인 전'],['incomplete','미완료 확인'],['complete','신청·결제 완료'],['stopped','신청·결제 중단']].map(([key,title])=>`<option value="${key}"${selected(c.payment,key)}>${title}</option>`).join('')}</select><small>최근 확인: ${fullFmt(c.payment_checked_at)}</small></label>`;
  return `<section class="card" id="case-conditions"><div class="card-header"><h2>확인할 항목</h2></div><div class="card-body"><form id="case-form">
    <div class="contact-facts"><strong>${esc(c.name||cu.name||'이름 미등록')}</strong><span>${esc(c.email||cu.email||c.phone||'연락처 미등록')}</span>${c.owner?`<small>담당 ${esc(c.owner)}</small>`:''}</div>
    <label class="checkbox"><input type="checkbox" name="email_allowed"${check(cu.email_allowed||c.email_allowed)}>이메일 후속 연락 허용 확인</label>
    ${cu.consent_note?`<p class="muted consent-summary">확인 근거: ${esc(cu.consent_note)}</p><details class="compact-details"><summary>연락 허용 근거 수정</summary><label class="field"><textarea name="consent_note" rows="2">${esc(cu.consent_note)}</textarea></label></details>`:`<label class="field">연락 허용 근거<textarea name="consent_note" rows="2" placeholder="수신 동의 등 확인한 근거를 기록하세요."></textarea></label>`}
    <label class="checkbox"><input type="checkbox" name="opt_out"${check(cu.opt_out||c.opt_out)}>고객이 연락 중단을 요청함</label>
    <label class="checkbox"><input type="checkbox" name="company_owes_info"${check(c.company_owes_info)}>회사가 확인한 뒤 답해야 함</label><p class="muted small-helper">선택하면 고객 무응답 후속을 시작하지 않습니다.</p>
    ${missing.length?`<div class="required-conditions"><h3>후속 발송에 필요한 정보</h3><p class="muted small-helper">확인된 항목만 입력하세요. 모르는 정보는 비워 두고 직원 확인 업무로 남깁니다.</p>${missing.map(key=>`<label class="field">${esc(key)}<input name="field:${esc(key)}" value=""></label>`).join('')}</div>`:''}
    ${c.state==='S6'?`<div class="required-conditions"><h3>신청·결제 확인</h3><p class="muted small-helper">S6 후속은 최신 미완료 확인과 건별 직원 승인 후에만 발송합니다.</p>${paymentField}</div>`:''}
    <details class="compact-details contact-details"><summary>고객정보 · 프로젝트 상세 수정</summary>
      <label class="field">고객 이름<input name="name" value="${esc(c.name||cu.name||'')}" required></label>
      <label class="field">이메일<input name="email" type="email" value="${esc(c.email||cu.email||'')}"></label>
      <label class="field">전화번호<input name="phone" type="tel" value="${esc(c.phone||cu.phone||'')}"></label>
      <label class="field">담당 직원<input name="owner" value="${esc(c.owner||'')}"></label>
      ${allFields.filter(key=>!missing.includes(key)).map(key=>`<label class="field">${esc(key)}<input name="field:${esc(key)}" value="${esc(fields[key]||'')}"></label>`).join('')}
      ${c.state!=='S6'?paymentField:''}
    </details>
    <details class="compact-details"><summary>내부 참고사항${c.notes?' · 기록 있음':''}</summary><label class="field"><textarea name="notes" rows="3">${esc(c.notes||'')}</textarea></label></details>
    <button class="btn full-width" type="submit">상담 정보 저장</button><small>답변은 별도로 자동 저장됩니다.</small>
  </form></div></section>`;
}

function nextContactHTML(c) {
  const activeJobs=(c.jobs||[]).filter(job=>['pending','staff_check'].includes(job.status)).sort((a,b)=>Date.parse(a.due_at)-Date.parse(b.due_at));
  const next=activeJobs[0];
  const operational=c.operational_status||{};
  const blockers=[...new Set(operational.blockers||[])];
  const nextAction=operational.next_action||(!c.state||c.selected_generation!==c.generation?'최신 문의를 읽고 고객 상태를 선택해 주세요.':c.drafts?.some(d=>d.status==='draft')?'답변을 검토하고 승인해 주세요.':next?'다음 연락 일정과 고객 답장 여부를 확인하세요.':'필요한 답변을 작성해 상담을 이어가세요.');
  return `<section class="card" id="next-followup"><div class="card-header"><h2>다음 연락 · 진행 상황</h2><button class="btn small" data-action="${c.followup_mode==='paused'?'resume-case':'pause-case'}"${!['active','paused'].includes(c.followup_mode)?' disabled':''}>${c.followup_mode==='paused'?'후속 재개':'후속 일시중지'}</button></div><div class="card-body compact-body">
    <div class="next-contact-summary"><div><small>다음 연락 예정</small><strong>${next?fullFmt(next.due_at):'예정된 후속 연락 없음'}</strong></div><div><small>현재 진행</small>${statusPill(c.workflow)}</div></div>
    ${blockers.length?`<div class="notice amber followup-blockers"><strong>발송 전 확인 필요</strong><ul>${blockers.map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul></div>`:''}
    <p class="next-task"><strong>다음 할 일</strong> ${esc(nextAction)}</p>
    ${next&&(next.activation_review_required||String(next.template_id).startsWith('S6'))?`<button class="btn section-gap" data-action="review-job" data-id="${esc(next.id)}">다음 예약 문구 확인·승인</button>`:''}
    <details class="compact-details schedule-details"><summary>전체 예약 ${c.jobs?.length||0}건 · 발송·취소 기록</summary>${jobTable(c.jobs||[],true)}</details>
  </div></section>`;
}


function pilotToolsHTML(c) {
  if(user.role!=='admin'||settings.mode!=='live'||c.purpose==='verification')return '';
  const limited=settings.operation_mode==='limited';
  return `<details class="card admin-extras"><summary class="card-body">관리자 · 제한 운영 ${c.pilot_enrolled?'대상 등록됨':'대상 설정'}</summary><div class="card-body"><p class="muted small-mb20">제한 운영은 관리자가 지정한 실제 상담 최대 5건에만 적용합니다. 자동 후속은 고객별로 따로 허용해야 합니다.</p>
    ${!limited?'<div class="notice">관리자 설정에서 제한 운영 단계를 선택한 뒤 등록할 수 있습니다.</div>':''}
    <dl class="definition"><dt>운영 대상</dt><dd>${c.pilot_enrolled?'등록됨':'미등록'}</dd><dt>자동 후속</dt><dd>${c.pilot_followups_enabled?'개별 허용됨':'개별 허용 전'}</dd><dt>문구 승인</dt><dd>${Number(c.operational_status?.approved_template_count||0)} / ${Number(c.operational_status?.total_template_count||0)}개 · 미승인 문구는 계속 보류</dd>${c.pilot_enrolled_at?`<dt>등록 확인</dt><dd>${fullFmt(c.pilot_enrolled_at)} · ${esc(c.pilot_enrolled_by||'')}</dd>`:''}</dl>
    <div class="actions"><button class="btn" data-action="pilot-enrollment" data-enabled="${c.pilot_enrolled?'false':'true'}"${!limited?' disabled':''}>${c.pilot_enrolled?'제한 운영 대상 해제':'이 상담을 제한 운영에 등록'}</button><button class="btn" data-action="pilot-followups" data-enabled="${c.pilot_followups_enabled?'false':'true'}"${!limited||!c.pilot_enrolled?' disabled':''}>${c.pilot_followups_enabled?'이 고객 자동 후속 중지':'이 고객 자동 후속 허용'}</button></div>
    <p class="muted small-mt14">후속 허용 전 연락 동의, 사용할 승인 문구와 치환 정보, 최근 직원 답장의 실제 발송을 확인합니다. 문구 승인 상태는 변경하지 않습니다.</p></div></details>`;
}

function verificationToolsHTML(c) {
  if(user.role!=='admin'||settings.mode!=='live')return '';
  return `<details class="card admin-extras"><summary class="card-body">관리자 · 실제 연결 확인용 상담</summary><div class="card-body"><p class="muted small-mb20">관리자가 지정한 확인용 수신 주소에만 적용합니다. 일반 고객의 예약과 분리하여 확인합니다.</p>${c.purpose==='verification'?'<span class="pill amber">별도 검증 상담</span><div class="actions section-gap"><button class="btn" data-action="verification-followup">확인용 후속 예약 1건</button></div>':'<button class="btn" data-action="mark-verification">확인용 상담으로 지정</button>'}</div></details>`;
}

async function renderCase(id) {
  await refreshDemo(id);
  [currentCase,templates] = await Promise.all([api(`/cases/${encodeURIComponent(id)}`),api('/templates')]);
  const c=currentCase, drafts=c.drafts||[], draft=drafts.find(d=>d.status==='draft');
  jobs=c.jobs||[];
  const oldDemo=demo.enabled; demo.enabled=false;
  const heading=pageHeading(esc(c.name||'고객 상담'),esc(c.subject||'전화 상담'),`<button class="btn" data-nav="cases">← 상담 목록</button>${settings.mode!=='live'&&c.email?'<button class="btn" data-action="mock-reply">모의 고객 답장</button>':''}`,'CUSTOMER DETAIL');
  demo.enabled=oldDemo;
  $('#main').innerHTML=heading+`<div class="detail-meta"><span>담당 ${esc(c.owner||'미지정')}</span><span>상담 #${esc(c.id)}</span></div>`+latestInquiryHTML(c)+
    (demo.enabled?`<details class="demo-tools"><summary>검수 도구 · 시간 이동 / 고객 답장 접수</summary>${demoPanel()}</details>`:'')+
    `<div class="split section-gap"><div class="consultation-flow">
      <section class="card" id="state-section"><div class="card-header"><h2>직원 상태 선택</h2>${statePill(c.state)}</div><div class="card-body">
      ${c.selected_generation!==c.generation?'<div class="notice amber">최신 문의를 읽고 상태를 다시 선택해 주세요. 이전 초안은 보존되지만 새 문의에 그대로 발송할 수 없습니다.</div>':'<p class="muted">같은 상태를 다시 저장해도 기존 예약과 기준 발송일은 유지됩니다.</p>'}
      <form id="state-form"><div class="state-grid">${Object.entries(stages).map(([s,n])=>`<label class="state-radio"><input type="radio" name="state" value="${s}"${check(c.state===s)} required><span><b>${s}</b>${n}</span></label>`).join('')}</div><button type="submit" class="btn primary">상태 저장</button></form></div></section>
      <section class="card" id="answer-section"><div class="card-header"><div><h2>답변 생성 · 편집 · 발송</h2><small>직원이 검토한 최종본만 발송합니다.</small></div><button class="btn" data-action="generate-draft"${!c.state||c.selected_generation!==c.generation||c.operational_status?.generation_blocked?' disabled':''}>${draft?'답변 재생성':'답변 생성'}</button></div><div class="card-body"><div id="generation-block" class="notice amber" role="status"${c.operational_status?.generation_blocked?'':' hidden'}>${esc(c.operational_status?.generation_block_reason||'')}</div>${draftHTML(draft,c)}${archivedDraftsHTML(drafts)}</div></section>
      ${nextContactHTML(c)}
      <details class="card" id="full-history"><summary class="card-body">전체 상담이력 · ${(c.history||[]).length}개의 기록</summary><div class="card-body">${historyHTML(c.history)}</div></details>
      ${c.state_history?.length?`<details class="card"><summary class="card-body">직원 상태 선택 이력</summary><div class="card-body"><ul class="compact-list">${c.state_history.map(s=>`<li>${statePill(s.state)} <span class="muted">${esc(s.actor||s.selected_by||'')} · ${fullFmt(s.created_at||s.selected_at)}</span></li>`).join('')}</ul></div></details>`:''}
    </div><aside>${caseInfoHTML(c)}${pilotToolsHTML(c)}${verificationToolsHTML(c)}${(c.outbox||[]).some(o=>['uncertain','sending'].includes(o.status))?`<section class="card"><div class="card-header"><h2>발송 결과 확인</h2></div><div class="card-body"><div class="notice amber">전송 여부가 확인될 때까지 추가 발송을 보류합니다.</div>${c.outbox.filter(o=>['uncertain','sending'].includes(o.status)).map(o=>`<p>${statusPill(o.status)} <button class="btn small" data-action="reconcile" data-id="${esc(o.id)}">Gmail 기록 확인</button></p>`).join('')}</div></section>`:''}</aside></div>`;
  if(draft)updateSaveStatus(draftBuffers.get(draft.id));
}

async function renderDashboard() {
  const [dashboard,rows]=await Promise.all([api('/dashboard'),api('/cases')]); cases=rows;
  const notes={new_inquiries:'새롭게 접수된 고객의 이야기',state_selection:'직원의 판단이 필요한 상담',draft_review:'발송 전 최종 확인',new_replies:'새 답장을 확인해 주세요',today_followups:'서울 시간 기준 오늘의 일정',sent_followups:'발송이 확인된 상담',staff_check:'조건과 진행 상태 확인',errors:'오류와 중단 사유 확인'};
  $('#main').innerHTML=pageHeading('오늘도, 좋은 변화를 함께.','고객의 이야기를 확인하고 다음 연락을 이어가세요.',`<button class="btn" data-action="poll-mail">↻ Gmail 문의 확인</button><button class="btn primary" data-action="new-phone">＋ 전화 문의 등록</button>`,'YOUR DAILY WORKSPACE')+
    `${dashboard.poll_error?'<div class="notice red">Gmail 문의 확인에 실패했습니다. 연결을 복구하기 전에는 자동 발송을 보류합니다.</div>':''}<section class="metrics" aria-label="업무 현황">${Object.entries(queueTitles).map(([key,name])=>`<button class="metric" data-nav="cases?queue=${key}" data-queue="${key}" aria-label="${name} ${Number(dashboard[key]||0)}건 보기"><span class="metric-top"><span>${name}</span><span aria-hidden="true">↗</span></span><span class="metric-num">${Number(dashboard[key]||0)}</span><span class="metric-note">${notes[key]}</span></button>`).join('')}</section>
    <section class="card"><div class="card-header"><div><h2>최근 상담</h2><small>고객 상태와 다음 연락을 한눈에 확인하세요.</small></div><button class="btn ghost small" data-nav="cases">전체 상담 보기 →</button></div>${caseTable(rows.slice(0,8))}</section>${settings.mode!=='live'?'<div class="notice">모의 운영 중입니다. 실제 고객에게 메일을 전송하지 않습니다. <button class="btn small" data-action="mock-inbound">모의 문의 접수</button></div>':''}`;
}

async function renderCases(queue='') {
  cases=await api(`/cases${queue?`?queue=${encodeURIComponent(queue)}`:''}`);
  $('#main').innerHTML=pageHeading(queueTitles[queue]||'고객 상담',queue?'대시보드와 같은 조건의 고객 목록입니다.':'고객의 문의부터 답변, 다음 연락까지 함께 관리합니다.',`<button class="btn" data-action="poll-mail">↻ 문의 확인</button>${queue?'<button class="btn" data-nav="cases">전체 상담 보기</button>':''}${settings.mode!=='live'?'<button class="btn primary" data-action="mock-inbound">＋ 모의 문의</button>':''}`)+
    `<div class="toolbar"><input class="search" id="case-search" type="search" placeholder="고객 이름, 이메일, 상담 제목 검색" aria-label="상담 검색"><select id="case-filter" aria-label="고객 상태 필터"><option value="">모든 고객 상태</option>${Object.entries(stages).map(([s,n])=>`<option value="${s}">${s} · ${n}</option>`).join('')}</select><span id="case-count" class="selection-count">총 ${cases.length}건</span></div><section class="card" id="case-table">${cases.length?caseTable(cases):empty('이 조건에 해당하는 상담이 없습니다','다른 업무 목록을 확인하거나 전체 상담에서 찾아보세요.')}</section>`;
}

function operationCards(operations) {
  const names={gmail_receive:'Gmail 실제 수신',openai_draft:'OpenAI 실제 초안',manual_send:'직원 승인 실제 발송',auto_followup:'무응답 자동 후속'};
  const states={waiting:'연결 대기',normal:'정상',stopped:'중지',error:'오류'};
  return `<div class="operation-grid">${Object.entries(names).map(([key,name])=>{
    const item=operations.capabilities?.[key]||{state:'waiting'};
    return `<div class="operation-card"><strong>${name}</strong><span class="pill ${item.state==='normal'?'teal':item.state==='error'?'red':'amber'}">${states[item.state]||'연결 대기'}${item.mode==='mock'?' · 모의':''}</span>${item.reason||item.error?`<small>${esc(item.reason||item.error)}</small>`:''}</div>`;
  }).join('')}</div>`;
}

async function renderSettings() {
  const [config,operations,gmail]=await Promise.all([api('/settings'),api('/operations'),api('/connections/gmail')]);
  settings=config; operationStatus=operations;
  const collection=gmail.collection||operations.collection||{};
  const start=collection.start_at?new Date(Date.parse(collection.start_at)+9*3600000).toISOString().slice(0,16):'';
  $('#main').innerHTML=pageHeading('관리자 설정','연결, 수신 범위, 직원 승인 발송과 자동 후속을 각각 확인합니다.')+
    operationCards(operations)+`<div class="split"><div>
    <section class="card"><div class="card-header"><h2>운영 · 발송 설정</h2><span class="pill ${settings.mode==='live'?'amber':'teal'}">${settings.mode==='live'?'실제 연동 모드':'모의 운영'}</span></div><div class="card-body"><form id="settings-form">
    <div class="form-grid"><label class="field">발송 시작 시각 (서울)<input name="business_start" type="number" min="0" max="23" value="${esc(settings.business_start??9)}" required></label><label class="field">발송 종료 시각 (서울)<input name="business_end" type="number" min="1" max="24" value="${esc(settings.business_end??18)}" required></label></div>
    <label class="field">발송 가능한 요일</label><div class="actions">${['월','화','수','목','금','토','일'].map((d,i)=>`<label class="checkbox"><input name="weekday" type="checkbox" value="${i}"${check((settings.weekdays||[0,1,2,3,4]).includes(i))}>${d}</label>`).join('')}</div>
    <label class="field">고객정보 보관기간 (일)<input name="retention_days" type="number" min="1" max="3650" value="${esc(settings.retention_days||365)}" required><small>삭제 실행과 백업 보관은 관리자 운영 절차를 따릅니다.</small></label>
    <label class="checkbox"><input name="legal_review_confirmed" type="checkbox"${check(settings.legal_review_confirmed)}>회사의 연락 및 광고성 이메일 발송 조건 검토를 완료했습니다.</label><hr class="divider">
    <label class="field">실제 연동 단계<select name="operation_mode"${settings.mode!=='live'?' disabled':''}><option value="verification"${selected(settings.operation_mode||'verification','verification')}>지정 수신자 연결 검증</option><option value="limited"${selected(settings.operation_mode,'limited')}>제한 운영 · 지정한 상담 최대 5건</option><option value="production"${selected(settings.operation_mode,'production')}>실제 운영</option></select></label>
    <label class="field">확인용 실제 수신 주소<input type="email" name="verification_recipient" value="${esc(settings.verification_recipient||'')}"${settings.mode!=='live'?' disabled':''}><small>본인이 사용할 실제 확인용 주소를 지정하세요. 별도 검증 상담에만 적용합니다.</small></label>
    <label class="checkbox"><input name="live_enabled" type="checkbox"${check(settings.live_enabled)}${settings.mode!=='live'?' disabled':''}>직원 승인 답장의 실제 Gmail 발송 활성화</label>
    <label class="checkbox"><input name="auto_followups_paused" type="checkbox"${check(settings.auto_followups_paused)}>전체 자동 후속 일시중지</label>
    <p class="muted small-mb20">연결 검증과 발송 조건 확인을 마친 뒤 활성화합니다. 실제 운영 전환은 확인용 메일 수신·답변·후속 발송 기록을 검토한 뒤 선택하세요.</p>
    <button class="btn primary" type="submit">운영 설정 저장</button></form><hr class="divider"><button class="btn danger" data-action="pause-all-followups">전체 자동 후속 중지</button><p class="muted small-mt14">새 자동 후속 발송만 중지합니다. 고객 상담과 기존 예약 기록은 보존됩니다.</p></div></section>
    <section class="card"><div class="card-header"><h2>실행 상태 · 활성화 확인</h2><button class="btn small" data-action="refresh">새로고침</button></div><div class="card-body"><dl class="definition"><dt>최근 수신 성공</dt><dd>${fullFmt(operations.telemetry?.gmail_poll?.last_success_at)}</dd><dt>최근 예약 실행</dt><dd>${fullFmt(operations.telemetry?.scheduler?.last_success_at)}</dd><dt>수신 오류</dt><dd>${esc(operations.telemetry?.gmail_poll?.error||'없음')}</dd><dt>예약 오류</dt><dd>${esc(operations.telemetry?.scheduler?.error||'없음')}</dd><dt>예약 실행기</dt><dd>${operations.scheduler_enabled?'실행 설정됨':'중지됨'}</dd><dt>최근 백업 성공</dt><dd>${fullFmt(operations.telemetry?.backup?.last_success_at)}</dd><dt>백업 오류</dt><dd>${esc(operations.telemetry?.backup?.error||'없음')}</dd><dt>최근 메일 등록 지연</dt><dd>${Number.isFinite(operations.telemetry?.gmail_poll?.details?.last_reception_latency_seconds)?Number(operations.telemetry.gmail_poll.details.last_reception_latency_seconds).toFixed(1)+'초 · 메일 수신 시각부터 사이트 등록까지':'측정된 새 메일 없음'}</dd></dl>${operations.activation?.missing?.length?`<div class="notice amber"><strong>실제 운영 전 확인할 항목</strong><ul>${operations.activation.missing.map(item=>`<li>${esc(activationLabels[item]||'추가 실제 연결 검증')}</li>`).join('')}</ul></div>`:''}</div></section>
    </div><div>
    <section class="card"><div class="card-header"><h2>Gmail 연결 · 수집 범위</h2></div><div class="card-body">
    <dl class="definition"><dt>등록된 계정</dt><dd>${esc(gmail.account_email||operations.sender||'연결 대기')}</dd><dt>OAuth 승인정보</dt><dd>${gmail.authorization_saved?'서버에 보관됨':'등록되지 않음'}</dd><dt>실제 Gmail 조회</dt><dd>${gmail.reconnect_required?'재연결 필요':gmail.read_verified?'현재 연결 조회 성공':gmail.authorization_saved?'승인정보 있음 · 실제 조회 확인 대기':gmail.mode==='mock'?'모의 모드 · 실제 연결 없음':'Google 인증 필요'}</dd><dt>최근 조회 검증</dt><dd>${fullFmt(gmail.last_read_verified_at)}</dd><dt>최근 수신 성공</dt><dd>${fullFmt(gmail.last_sync?.last_success_at)}</dd><dt>최근 수신 시도</dt><dd>${fullFmt(gmail.last_sync?.last_attempt_at)}</dd><dt>인증 복귀 주소</dt><dd>${esc(gmail.redirect_uri||'프로그램의 APP_BASE_URL 설정 필요')}</dd><dt>연결·수신 오류</dt><dd>${esc(gmail.error||gmail.last_sync?.error||'없음')}</dd></dl>
    ${gmail.reconnect_required?'<div class="notice amber">Google 인증을 다시 승인해야 합니다. Gmail을 재연결한 뒤 실제 조회 결과를 확인하세요.</div>':gmail.authorization_saved&&!gmail.read_verified?'<div class="notice amber">저장된 승인정보만으로 연결 완료를 판단하지 않습니다. 아래 연결 검증을 실행하여 Gmail 조회 성공 여부를 확인하세요.</div>':''}
    <button class="btn primary" data-action="gmail-connect"${demo.enabled?' disabled':''}>${gmail.authorization_saved||gmail.reconnect_required?'Gmail 재연결':'Gmail 연결'}</button>
    <p class="muted small-mt14">Google 화면에서 관리자가 계정을 선택하고 승인합니다. 직원 사이트 로그인과 Gmail 연결은 별도입니다.</p><hr class="divider">
    <form id="gmail-collection-form"><label class="field">수집 대상 Gmail 라벨<input name="label" value="${esc(collection.label||'한국갭이어/문의')}" required${demo.enabled?' disabled':''}></label><label class="field">새 문의 수집 시작 시점 · 서울<input name="start_at" type="datetime-local" value="${esc(start)}" required${demo.enabled?' disabled':''}></label><button class="btn" type="submit"${demo.enabled?' disabled':''}>범위와 건수 먼저 확인</button></form>
    <p class="muted small-mt14">지정 라벨과 시작 시점에 맞는 새 문의만 수집하며, 등록된 상담의 답장은 계속 추적합니다. 라벨은 프로그램의 처리 범위이며 Google 접근 권한 자체를 제한하지 않습니다.</p>
    </div></section>
    <section class="card"><div class="card-header"><h2>OpenAI · Sheets 연결</h2></div><div class="card-body">
    <p class="muted text-mb16">API 키와 인증정보는 프로그램을 실행하는 컴퓨터의 비밀 설정 파일 또는 서버 환경변수로 등록합니다. 이 화면과 브라우저 저장소에 비밀값을 입력하지 않습니다. Gmail 승인 대기 중에도 OpenAI 연결 검증을 실행할 수 있습니다.</p>
    <div class="settings-status" id="connection-status">${esc(connectionText(settings.connection_status))}</div>
    ${operations.openai&&operations.openai.mode!=='mock'?`<dl class="definition"><dt>OpenAI 모델</dt><dd>${esc(operations.openai.model||'설정 필요')}</dd><dt>오늘 요청</dt><dd>${Number(operations.openai.requests_used||0)} / ${Number(operations.openai.daily_requests||0)}회</dd><dt>오늘 토큰</dt><dd>${Number(operations.openai.tokens_used_or_reserved||0)} / ${Number(operations.openai.daily_tokens||0)}</dd><dt>최근 요청</dt><dd>${esc(operations.openai.last_request?.request_id||'없음')}</dd></dl>${operations.openai.alert?'<div class="notice amber">설정한 사용량 알림 기준에 도달했습니다.</div>':''}<p class="muted small-mb20">${esc(operations.openai.budget_note||'사용량은 서울 날짜 기준입니다. 연결 검증 요청도 사용량에 포함됩니다.')}</p><form id="openai-limits-form"><div class="form-grid"><label class="field">일일 요청 제한<input type="number" name="daily_requests" min="1" value="${Number(operations.openai.daily_requests||100)}" required></label><label class="field">일일 토큰 제한<input type="number" name="daily_tokens" min="1" value="${Number(operations.openai.daily_tokens||250000)}" required></label></div><label class="field">화면 알림 기준 (%)<input type="number" name="alert_percent" min="1" max="100" value="${Number(operations.openai.alert_percent||80)}" required></label><button class="btn" type="submit">사용량 제한 저장</button></form>`:''}
    <div class="actions section-gap"><button class="btn" data-action="verify-connections"${demo.enabled?' disabled':''}>실제 요청으로 연결 검증</button><button class="btn" data-action="sheets-sync"${demo.enabled?' disabled':''}>Sheets 보고 데이터 동기화</button></div>
    <p class="muted small-mt14">상담·초안·예약·문구의 기준 저장소는 서버 데이터베이스입니다. Sheets는 SQLite → Sheets 단방향 보고용이며, 시트의 문구 수정은 사이트에 반영되지 않습니다.</p>
    ${demo.enabled?'<div class="notice section-gap">직접 검수 모드에서는 외부 API 연결과 실제 발송이 차단됩니다.</div>':''}
    </div></section>
    <section class="card"><div class="card-header"><h2>감사 기록</h2><button class="btn small" data-action="load-audit">기록 조회</button></div><div class="card-body" id="audit-content"><p class="muted text12">직원의 상태 선택, 문구 수정·승인, 발송 작업을 확인합니다.</p></div></section>
    </div></div>`;
}

async function reviewSend(id) {
  const buffer=draftBuffers.get(id);
  if (!buffer?.body.trim()) throw new Error('발송할 답변을 입력해 주세요.');
  await saveDraftBuffer(buffer);
  const [fresh,operations]=await Promise.all([api(`/cases/${encodeURIComponent(currentCase.id)}`),api('/operations')]);
  const serverDraft=fresh.drafts.find(d=>d.id===id);
  if (!serverDraft || serverDraft.status!=='draft' || serverDraft.edit_version!==buffer.version || fresh.generation!==currentCase.generation || fresh.state_revision!==currentCase.state_revision) {
    buffer.conflict=true; buffer.status='conflict'; updateSaveStatus(buffer);
    throw new Error('다른 직원의 수정 또는 새 고객 문의가 있습니다. 최신 내용을 확인한 뒤 다시 승인해 주세요.');
  }
  const approval={email:fresh.email,subject:fresh.subject,generation:fresh.generation,state_revision:fresh.state_revision};
  const actual=settings.mode==='live';
  openModal('최종 답변 확인',`<div class="notice ${actual?'amber':''}">${actual?'직원이 승인하면 아래 계정으로 실제 이메일을 발송합니다.':'모의 발송입니다. 실제 고객에게 전송되지 않습니다.'}</div><dl class="definition"><dt>보내는 계정</dt><dd>${actual?esc(operations.sender||'Gmail 연결 대기'):'모의 Gmail'}</dd><dt>받는 사람</dt><dd>${esc(fresh.email)}</dd><dt>제목</dt><dd>${esc(fresh.subject)}</dd></dl><div class="message"><div class="message-body">${esc(buffer.body)}</div></div><form id="send-form" data-id="${esc(id)}" data-version="${Number(buffer.version)}"><label class="checkbox"><input type="checkbox" required>수신자·발신 계정·제목·최종 본문과 확인할 조건을 검토했으며 발송을 승인합니다.</label><button class="btn primary" type="submit">승인하고 ${actual?'실제':'모의'} 발송</button></form>`);
  $('#send-form').dataset.approval=JSON.stringify(approval);
}

async function navigate() {
  if (!user) return;
  const hash=location.hash.replace(/^#/,'')||'dashboard';
  if (activeRoute && !(await guardDraftNavigation())) {
    history.replaceState(null,'',`#${activeRoute}`); return;
  }
  const [path,query='']=hash.split('?'), [route,id]=path.split('/');
  if (!['dashboard','cases','case','jobs','templates','calls','settings'].includes(route)) {location.hash='dashboard';return;}
  if (route==='settings' && user.role!=='admin') {location.hash='dashboard';return;}
  currentPage=route==='case'?'cases':route;
  if (!$('#main')) shell();
  activeRoute=hash;
  $$('[data-nav]',$('.sidebar')).forEach(b=>b.classList.toggle('active',b.dataset.nav===currentPage));
  $('#breadcrumb-page').textContent=({dashboard:'대시보드',cases:'고객 상담',case:'고객 상세',jobs:'후속관리 현황',templates:'후속 문구',calls:'전화 문의',settings:'관리자 설정'})[route];
  $('#main').innerHTML='<div class="loading">업무 정보를 불러오고 있습니다…</div>';
  try {
    await refreshDemo(route==='case'?id:undefined);
    await ({dashboard:renderDashboard,cases:()=>renderCases(new URLSearchParams(query).get('queue')||''),case:()=>renderCase(id),jobs:renderJobs,templates:renderTemplates,calls:renderCalls,settings:renderSettings})[route]();
    if(route!=='case')currentCase=null;
  } catch(error) {
    $('#main').innerHTML=`<div class="notice red">${esc(error.message)}</div><button class="btn" data-action="refresh">다시 불러오기</button>`;
  }
}

async function generateDraft() {
  const current=currentCase.drafts?.find(d=>d.status==='draft');
  const buffer=current?draftBuffers.get(current.id):null;
  if (current && !window.confirm('기존 답변은 이전 초안으로 보존하고 새 답변을 생성합니다. 현재 수정 내용을 저장한 뒤 계속할까요?')) return;
  if (buffer) await saveDraftBuffer(buffer);
  await api(`/cases/${currentCase.id}/draft`,'POST',{regenerate:!!current,expected_version:buffer?.version,expected_generation:currentCase.generation});
  await renderCase(currentCase.id);
  toast('답변 초안을 준비했습니다. 직원 확인 사항과 본문을 검토해 주세요.');
}

function affectedJobText(rows) {
  return rows.length?rows.map(j=>`${j.template_id} · ${fullFmt(j.due_at)} · ${labels[j.status]||j.status}`).join('\n'):'영향받는 미발송 예약 없음';
}

async function saveState(data) {
  const preview=await api(`/cases/${currentCase.id}/state-impact?state=${encodeURIComponent(data.state)}`);
  if (preview.requires_confirmation && !window.confirm(`${preview.reason}\n\n영향받는 예약 ${preview.affected_jobs.length}건\n${affectedJobText(preview.affected_jobs)}\n\n상태를 변경할까요? 새 후속 일정은 새 상담 답변이 발송된 뒤 생성됩니다.`)) return;
  const buffer=draftBuffers.get($('#draft-form')?.dataset.id);
  if (buffer?.dirty) await saveDraftBuffer(buffer);
  await api(`/cases/${currentCase.id}/state`,'POST',{state:data.state,expected_revision:preview.state_revision,expected_generation:preview.generation,confirm_change:!!preview.requires_confirmation});
  await renderCase(currentCase.id);
  toast(preview.changed?'직원이 선택한 상태를 저장했습니다.':'같은 상태를 유지했습니다. 기존 예약과 발송 기준일은 그대로입니다.');
}

function updateGenerationStatus(c) {
  const button=$('[data-action=generate-draft]');
  const notice=$('#generation-block');
  const blocked=!!c.operational_status?.generation_blocked;
  if(button)button.disabled=!c.state||c.selected_generation!==c.generation||blocked;
  if(notice){notice.hidden=!blocked;notice.textContent=c.operational_status?.generation_block_reason||'';}
}

async function saveCaseInfo(form,data,fd) {
  const fields={...(currentCase.fields||{})};
  for(const[k,v]of fd)if(k.startsWith('field:'))fields[k.slice(6)]=v;
  const paymentChanged=fd.has('payment')&&data.payment!==currentCase.payment;
  const changesFollowups=(fd.has('opt_out')&&!currentCase.customer?.opt_out)||(paymentChanged&&['complete','stopped'].includes(data.payment));
  const pending=(currentCase.jobs||[]).filter(j=>!['sent','cancelled','canceled','uncertain','sending'].includes(j.status));
  if(changesFollowups&&pending.length&&!window.confirm(`연락 조건 변경으로 후속 예약 ${pending.length}건이 중지되거나 취소됩니다.\n${affectedJobText(pending)}\n변경 내용을 저장할까요?`))return;
  const result=await api(`/cases/${currentCase.id}`,'PATCH',{
    name:data.name,email:data.email||null,phone:data.phone,owner:data.owner,
    email_allowed:fd.has('email_allowed'),opt_out:fd.has('opt_out'),consent_note:data.consent_note,
    fields,notes:data.notes,company_owes_info:fd.has('company_owes_info'),...(paymentChanged?{payment:data.payment}:{})
  });
  currentCase=result;
  jobs=result.jobs||jobs;
  updateGenerationStatus(result);
  // The answer editor is deliberately not replaced when contact information is saved.
  const schedule=$('#next-followup');
  const scheduleOpen=!!$('#next-followup .schedule-details')?.open;
  if(schedule){schedule.outerHTML=nextContactHTML(result);const details=$('#next-followup .schedule-details');if(details)details.open=scheduleOpen;}
  const buffer=draftBuffers.get($('#draft-form')?.dataset.id);
  if(buffer) {
    const updated=result.drafts?.find(d=>d.id===buffer.id);
    if(updated&&updated.status!=='draft')buffer.stale=true;
    updateSaveStatus(buffer);
    if(buffer.dirty&&!buffer.stale)saveDraftBuffer(buffer).catch(()=>{});
  }
  toast('상담 정보를 저장했습니다. 편집 중인 답변은 그대로 유지됩니다.');
}

async function verificationFollowupModal() {
  const approved=(await api('/templates')).filter(t=>t.state===currentCase.state&&['approved','승인 완료'].includes(t.approval));
  const start=new Date(Date.now()+5*60000+9*3600000).toISOString().slice(0,16);
  openModal('확인용 후속 예약 1건',`<div class="notice amber">확인용 수신자의 별도 검증 상담에만 예약합니다. 일반 고객의 일정과 현재 시각은 바꾸지 않습니다. 문구의 기존 승인 상태와 발송 조건은 그대로 적용됩니다.</div><form id="verification-job-form"><label class="field">현재 상태의 승인된 문구<select name="template_id" required><option value="">문구 선택</option>${approved.map(t=>`<option value="${esc(t.id)}">${esc(t.id)} · 버전 ${Number(t.version)}</option>`).join('')}</select></label><label class="field">확인용 발송 시각 · 서울<input type="datetime-local" name="due_at" value="${start}" required></label><label class="checkbox"><input type="checkbox" required>확인용 수신자와 승인 문구를 검토했습니다.</label><button class="btn primary" type="submit">확인용 예약 생성</button></form>`);
}

document.addEventListener('click',event=>{
  const button=event.target.closest('button');
  if(!button)return;
  if(button.dataset.nav){closeModal();if(location.hash===`#${button.dataset.nav}`)navigate();else location.hash=button.dataset.nav;return;}
  const action=button.dataset.action;
  if(!action)return;
  runButton(button,async()=>{
    const id=button.dataset.id;
    switch(action){
      case 'demo-advance': {
        const result=await api('/demo/advance','POST',{day:Number(button.dataset.day),case_id:id});
        await showDemoCase(id);toast(`테스트 시각을 ${fullFmt(result.now||result.effective_at)}으로 이동했습니다.`);break;
      }
      case 'demo-reply':demoReplyModal(id);break;
      case 'demo-scenario':{
        if(!(await guardDraftNavigation()))return;
        const result=await api('/demo/scenario','POST',{}),caseId=result.case_id||result.id||result.case?.id;
        if(!caseId)throw new Error('검수 문의 생성 결과를 확인할 수 없습니다.');
        location.hash=`case/${caseId}`;toast('새 검수 문의를 등록했습니다. 직원이 상태를 선택해 주세요.');break;
      }
      case 'logout':
        if(!(await guardDraftNavigation()))return;
        await api('/logout','POST',{});user=null;activeRoute='';draftBuffers.clear();renderLogin();break;
      case 'close-modal':closeModal();break;
      case 'refresh':closeModal();await navigate();break;
      case 'new-phone':phoneModal();break;
      case 'mock-inbound':mockModal();break;
      case 'mock-reply':mockModal(true);break;
      case 'poll-mail':{
        if(!(await guardDraftNavigation()))return;
        const result=await api('/mail/poll','POST',{});
        if(result.ok===false)throw new Error(result.error||'메일 확인에 실패했습니다.');
        toast(`메일 확인 완료${typeof result.count==='number'?` · ${result.count}건`:''}`);await navigate();break;
      }
      case 'generate-draft':await generateDraft();break;
      case 'review-send':await reviewSend(id);break;
      case 'resolve-draft':await resolveDraftConflict();break;
      case 'discard-local-draft':{
        if(!window.confirm('미저장 작성 내용을 필요한 곳에 복사했습니까? 서버에 보존된 이전 초안은 유지되며, 이 창의 미저장 내용은 닫힙니다.'))return;
        const buffer=draftBuffers.get(id);if(buffer){clearTimeout(buffer.timer);draftBuffers.delete(id);}
        closeModal();await renderCase(currentCase.id);break;
      }
      case 'pause-case':case 'resume-case':{
        const fresh=await api(`/cases/${currentCase.id}`);
        const pending=(fresh.jobs||[]).filter(job=>!['sent','cancelled','canceled','uncertain','sending'].includes(job.status));
        const resume=action==='resume-case';
        const prompt=resume?`고객의 자동 후속을 재개합니다. 기존 기준일과 일정은 유지합니다.\n영향받는 예약 ${pending.length}건\n${affectedJobText(pending)}\n\n시점이 지난 예약은 개별 검토·승인하기 전까지 발송되지 않습니다. 회사 확인 대기·연락 거부 등 발송 조건도 계속 적용됩니다. 재개할까요?`:`고객의 자동 후속을 일시중지합니다.\n대상 예약 ${pending.length}건\n${affectedJobText(pending)}\n\n중지 사유: 직원이 고객 상황을 확인하기 위해 후속 연락을 보류함\n계속할까요?`;
        if(!window.confirm(prompt))return;
        await api(`/cases/${currentCase.id}`,'PATCH',{followup_mode:resume?'active':'paused'});
        await renderCase(currentCase.id);
        toast(resume?'후속을 재개했습니다. 시점이 지난 예약은 문구 확인에서 한 건씩 승인해 주세요.':'이 고객의 후속 연락을 일시중지했습니다.');break;
      }
      case 'edit-template':templateModal(id);break;
      case 'approve-template':case 'revoke-template':{
        const template=templates.find(t=>t.id===id);
        if($('#template-form')?.elements.body.value!==template.body)throw new Error('수정한 문구를 먼저 저장한 후 승인 상태를 변경해 주세요.');
        await api(`/templates/${id}/approval`,'POST',{approved:action==='approve-template'});closeModal();await renderTemplates();toast(action==='approve-template'?'문구를 승인했습니다.':'문구 승인을 취소했습니다.');break;
      }
      case 'review-job':await reviewJob(id);break;
      case 'complete-call':await api(`/calls/${id}/complete`,'POST',{});await renderCalls();toast('전화 연락을 완료 처리했습니다.');break;
      case 'reconcile':{
        const result=await api(`/outbox/${id}/reconcile`,'POST',{});await navigate();toast(result.status==='sent'?'Gmail에서 발송 완료를 확인했습니다.':'Gmail에서 발송 여부를 확인하지 못했습니다. 직원 확인 상태를 유지합니다.',result.status!=='sent');break;
      }
      case 'verify-connections':{
        const result=await api('/connections/verify','POST',{});await renderSettings();toast(result.openai?.verified&&!result.connectivity_verified?'OpenAI 실제 생성 확인 성공 · Gmail 연결은 별도 확인이 필요합니다.':result.connectivity_verified?'외부 서비스 요청 결과를 확인했습니다.':'연결 결과와 필요한 설정을 확인해 주세요.');break;
      }
      case 'gmail-connect':{
        if(!(await guardDraftNavigation()))return;
        const result=await api('/connections/gmail/start','POST',{});
        const url=new URL(result.authorization_url);
        if(url.protocol!=='https:'||url.hostname!=='accounts.google.com')throw new Error('올바른 Google 인증 주소를 확인하지 못했습니다.');
        location.assign(url.href);break;
      }
      case 'pause-all-followups':await api('/operations/auto-followups/pause','POST',{});await renderSettings();toast('전체 자동 후속을 중지했습니다. 상담과 예약 기록은 보존됩니다.');break;
      case 'sheets-sync':{
        const result=await api('/sheets/sync','POST',{});toast(result.message||'Google Sheets 보고 데이터를 동기화했습니다.');break;
      }
      case 'pilot-enrollment':{
        const enabled=button.dataset.enabled==='true';
        if(!window.confirm(`${currentCase.name||'고객'} · ${currentCase.email||'이메일 없음'}\n\n${enabled?'이 상담을 최대 5건의 상담 제한 운영 대상으로 등록할까요? 직원 승인 발송만 가능하며 자동 후속은 별도로 허용해야 합니다.':'제한 운영 대상을 해제할까요? 이 고객의 새로운 실제 발송을 차단합니다.'}`))return;
        await api(`/cases/${currentCase.id}/pilot`,'POST',{enabled});await renderCase(currentCase.id);toast(enabled?'제한 운영 대상에 등록했습니다.':'제한 운영 대상에서 해제했습니다.');break;
      }
      case 'pilot-followups':{
        const enabled=button.dataset.enabled==='true';
        if(!window.confirm(`${currentCase.name||'고객'} · ${currentCase.email||'이메일 없음'}\n\n${enabled?'이 고객의 무응답 자동 후속을 허용할까요? 현재 선택 상태의 승인 문구와 확인된 발송 조건만 사용합니다. S6은 건별 승인이 계속 필요합니다.':'이 고객의 새로운 자동 후속 발송을 중지할까요? 상담과 기존 기록은 보존됩니다.'}`))return;
        await api(`/cases/${currentCase.id}/pilot-followups`,'POST',{enabled});await renderCase(currentCase.id);toast(enabled?'이 고객의 자동 후속을 개별 허용했습니다.':'이 고객의 자동 후속을 중지했습니다.');break;
      }
      case 'mark-verification':
        if(!window.confirm('관리자 설정의 확인용 실제 수신 주소와 일치하는 별도 검증 상담으로 지정합니다. 계속할까요?'))return;
        await api(`/cases/${currentCase.id}/verification`,'POST',{});await renderCase(currentCase.id);toast('확인용 검증 상담으로 지정했습니다.');break;
      case 'verification-followup':await verificationFollowupModal();break;
      case 'load-audit':{
        const rows=await api('/audit');
        $('#audit-content').innerHTML=rows.length?`<ul class="compact-list">${rows.slice(0,100).map(r=>`<li><strong>${esc(r.action)}</strong><br><small>${esc(r.actor||'시스템')} · ${fullFmt(r.created_at)} · ${esc(r.object_id||r.target_id||r.entity_id||'')}</small>${r.details?`<p class="muted">${esc(typeof r.details==='object'?JSON.stringify(r.details):r.details)}</p>`:''}</li>`).join('')}</ul>`:'<p class="muted">아직 감사 기록이 없습니다.</p>';break;
      }
    }
  });
});

document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!form.id)return;
  event.preventDefault();
  const button=event.submitter||$('button[type=submit]',form);
  if(button?.disabled)return;
  if(button)button.disabled=true;
  try{
    const fd=new FormData(form),data=Object.fromEntries(fd);
    switch(form.id){
      case 'login-form':
        await api('/login','POST',data);user=await api('/me');settings=await api('/settings');shell();await navigate();break;
      case 'demo-reply-form':{
        const buffer=draftBuffers.get($('#draft-form')?.dataset.id);
        if(buffer?.dirty)await saveDraftBuffer(buffer);
        await api('/demo/reply','POST',{case_id:form.dataset.id,body:data.body});closeModal();await showDemoCase(form.dataset.id);toast('고객 답장을 접수했습니다. 예약 취소와 상태 재선택 안내를 확인해 주세요.');break;
      }
      case 'state-form':await saveState(data);break;
      case 'case-form':await saveCaseInfo(form,data,fd);break;
      case 'draft-form':{
        editDraftBody(form);await saveDraftBuffer(draftBuffers.get(form.dataset.id));toast('수정된 답변을 저장했습니다.');break;
      }
      case 'draft-merge-form':{
        const buffer=draftBuffers.get(form.dataset.id);
        Object.assign(buffer,{body:data.body,version:Number(form.dataset.version),dirty:true,conflict:false,status:'unsaved'});
        if($('#draft-form')?.dataset.id===buffer.id)$('#draft-form textarea').value=data.body;
        await saveDraftBuffer(buffer,true);closeModal();toast('비교하여 확인한 답변을 저장했습니다.');break;
      }
      case 'send-form':{
        const result=await api(`/drafts/${form.dataset.id}/send`,'POST',{expected_version:Number(form.dataset.version),approval:JSON.parse(form.dataset.approval)});
        closeModal();await renderCase(currentCase.id);toast(result.status==='sent'?'승인된 답변의 발송을 완료했습니다.':(result.reason||result.error||'발송 결과를 확인해 주세요.'),result.status!=='sent');break;
      }
      case 'phone-form':{
        const result=await api('/calls','POST',{...data,email:data.email||null,email_allowed:fd.has('email_allowed'),consulted_at:utcFromSeoul(data.consulted_at),followup_at:utcFromSeoul(data.followup_at)});
        closeModal();toast('전화 상담을 등록했습니다.');const id=result.case_id||result.id||result.case?.id;if(id)location.hash=`case/${id}`;else await navigate();break;
      }
      case 'mock-form':
        await api('/mock/inbound','POST',data);closeModal();toast('모의 고객 메일을 접수했습니다.');await navigate();break;
      case 'template-form':
        await api(`/templates/${form.dataset.id}`,'PUT',{body:data.body});closeModal();await renderTemplates();toast('새 버전을 승인 대기로 저장했습니다.');break;
      case 'job-form':{
        const isS6=form.dataset.s6==='true';
        if(isS6)await api(`/cases/${form.dataset.case}`,'PATCH',{payment:data.payment});
        let result;
        if(!isS6||data.payment==='incomplete')result=await api(`/jobs/${form.dataset.id}/approve`,'POST',{});
        closeModal();await navigate();
        toast(isS6&&data.payment!=='incomplete'?'신청·결제 상태를 반영하고 후속 연락을 중단했습니다.':result?.status==='sent'?'승인한 예약 한 건의 발송을 완료했습니다.':'승인 결과를 반영했습니다. 예약 상태를 확인해 주세요.');break;
      }
      case 'settings-form':{
        const payload={business_start:Number(data.business_start),business_end:Number(data.business_end),weekdays:fd.getAll('weekday').map(Number),retention_days:Number(data.retention_days),legal_review_confirmed:fd.has('legal_review_confirmed'),live_enabled:fd.has('live_enabled'),auto_followups_paused:fd.has('auto_followups_paused')};
        if(settings.mode==='live'){payload.operation_mode=data.operation_mode;payload.verification_recipient=data.verification_recipient||'';}
        if(!payload.weekdays.length)throw new Error('발송 가능한 요일을 하나 이상 선택하세요.');
        if(payload.operation_mode==='limited'&&settings.operation_mode!=='limited'&&!window.confirm('실제 연결 검증 기록을 확인했습니까? 관리자가 지정하는 최대 5건의 상담 제한 운영으로 전환합니다. 자동 후속은 고객별로 별도 허용해야 합니다.'))return;
        if(payload.operation_mode==='production'&&settings.operation_mode!=='production'&&!window.confirm('실제 연결 검증과 확인용 발송 기록을 확인했습니까? 승인된 범위의 실제 운영으로 전환합니다.'))return;
        await api('/settings','PUT',payload);await renderSettings();toast('운영 설정을 저장했습니다.');break;
      }
      case 'openai-limits-form':
        await api('/connections/openai/limits','PUT',{daily_requests:Number(data.daily_requests),daily_tokens:Number(data.daily_tokens),alert_percent:Number(data.alert_percent)});await renderSettings();toast('OpenAI 사용량 제한을 저장했습니다.');break;
      case 'gmail-collection-form':{
        const payload={label:data.label,start_at:utcFromSeoul(data.start_at)};
        const result=await api('/connections/gmail/collection/preview','POST',payload);
        openModal('Gmail 수집 범위 확인',`<dl class="definition"><dt>라벨</dt><dd>${esc(result.label||payload.label)}</dd><dt>시작 시점</dt><dd>${fullFmt(result.start_at||payload.start_at)}</dd><dt>대상 메일</dt><dd>${Number(result.count||0)}건</dd></dl><div class="notice amber">이 범위의 메일이 직원 사이트에 고객 문의로 등록될 수 있습니다. 개인 메일이 포함되지 않았는지 확인하세요.</div><form id="gmail-collection-confirm"><label class="checkbox"><input type="checkbox" required>수집 범위와 건수를 확인했습니다.</label><button class="btn primary" type="submit">확인한 범위 적용</button></form>`);
        $('#gmail-collection-confirm').dataset.payload=JSON.stringify({label:result.label||payload.label,start_at:result.start_at||payload.start_at,preview_token:result.preview_token});break;
      }
      case 'gmail-collection-confirm':
        await api('/connections/gmail/collection','PUT',JSON.parse(form.dataset.payload));closeModal();await renderSettings();toast('확인한 Gmail 수집 범위를 적용했습니다.');break;
      case 'verification-job-form':
        await api(`/cases/${currentCase.id}/verification-followup`,'POST',{template_id:data.template_id,due_at:utcFromSeoul(data.due_at)});closeModal();await renderCase(currentCase.id);toast('별도의 확인용 후속 예약을 생성했습니다.');break;
    }
  }catch(error){
    if(form.id==='login-form')$('#login-error').textContent=error.message;
    else toast(error.message,true);
  }finally{if(button?.isConnected)button.disabled=false;}
});

document.addEventListener('input',event=>{
  if(event.target.id==='case-search')filterCases();
  if(event.target.id==='template-search')filterTemplates();
  if(event.target.matches('#draft-form textarea[name=body]'))editDraftBody(event.target.form);
});
document.addEventListener('change',event=>{
  if(event.target.id==='case-filter')filterCases();
  if(['template-filter','approval-filter'].includes(event.target.id))filterTemplates();
  if(event.target.id==='job-filter')$('#jobs-table').innerHTML=jobTable(jobs.filter(j=>!event.target.value||j.status===event.target.value));
});
window.addEventListener('beforeunload',event=>{
  if([...draftBuffers.values()].some(b=>b.dirty||b.pending)){event.preventDefault();event.returnValue='';}
});
window.addEventListener('hashchange',navigate);
(async()=>{
  try{user=await api('/me');settings=await api('/settings');shell();await navigate();}
  catch{renderLogin();}
})();
