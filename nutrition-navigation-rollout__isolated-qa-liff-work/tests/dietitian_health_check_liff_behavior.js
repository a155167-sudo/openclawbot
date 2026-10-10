'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.textContent = '';
    this.className = '';
    this.disabled = false;
    this.attributes = {};
    this.value = '';
    this.hidden = false;
    this.dataset = {};
    this.parentElement = null;
  }
  append(...children) { for (const child of children) { if (child instanceof Element) child.parentElement = this; this.children.push(child); } }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { for (const child of this.children) if (child instanceof Element) child.parentElement = null; this.children = []; this.append(...children); }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() {}
  async dispatch(type, event = {}) {
    for (const handler of this.listeners[type] || []) await handler({type, target: this, ...event});
    await settle();
  }
}

function walk(root) {
  return [root, ...root.children.flatMap(child => child instanceof Element ? walk(child) : [])];
}
function find(root, predicate) { return walk(root).find(predicate); }
function renderedText(root) { return root.textContent + root.children.map(child => child instanceof Element ? renderedText(child) : String(child)).join(''); }
function isActuallyVisible(element) {
  for (let current = element; current; current = current.parentElement) if (current.hidden) return false;
  return true;
}
function renderedTextOutsideDetails(root) {
  return [root, ...root.children.flatMap(child => {
    if (!(child instanceof Element) || child.tagName === 'DETAILS') return [];
    return walk(child);
  })].map(element => element.textContent).join('\n');
}
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}
async function settle() {
  for (let i = 0; i < 8; i += 1) await new Promise(resolve => setImmediate(resolve));
}
function jsonResponse(body, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => body};
}
function imageResponse(status = 200) {
  return {ok: status >= 200 && status < 300, status, blob: async () => ({kind: 'jpeg'})};
}

const script = fs.readFileSync(process.argv[2], 'utf8');
const apiFixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const listing = {items: [{case_id: 'case/A', status: 'ready_for_review', valid_day_count: 3, window_started_at: 'start', window_ends_at: 'end'}]};
function validDay(localDate, completenessStatus = 'qualified', qualifyingMealCount = 2) {
  return {
    local_date: localDate,
    rule_version: 'draft-confirmed-meals-v1',
    qualifying_meal_count: qualifyingMealCount,
    completeness_status: completenessStatus,
    evaluated_at: `${localDate}T12:00:00+08:00`,
  };
}
const threeQualifiedDays = [validDay('2026-09-02'), validDay('2026-09-03'), validDay('2026-09-04')];
const detail = {
  case_id: 'case/A',
  status: 'ready_for_review',
  valid_day_count: 3,
  valid_days: threeQualifiedDays,
  source_logs: [{
    log_id: 'log B', trust_type: 'user_confirmed_ai_estimate',
    estimate_schema_version: 'meal-photo-user-confirmation-v2',
    nutrition_snapshot: {calories_kcal: 680, protein_g: 35},
    estimate: {
      calories_kcal: 680, protein_g: 35,
      calories_kcal_range: {min: 580, max: 800, basis: 'ai_vision_estimate_range_v1'},
      protein_g_range: {min: 29, max: 43, basis: 'ai_vision_estimate_range_v1'},
      fat_g: null, carbohydrate_g: null,
    },
  }],
  source_integrity: {referenced_count: 1, available_snapshot_count: 1, all_snapshots_available: true},
};

function fakeClock() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    setTimeout(fn, delay) { const id = nextId++; timers.set(id, {at: now + delay, fn}); return id; },
    clearTimeout(id) { timers.delete(id); },
    advance(ms) {
      now += ms;
      for (;;) {
        const due = [...timers].filter(([, timer]) => timer.at <= now).sort((a, b) => a[1].at - b[1].at || a[0] - b[0]);
        if (!due.length) break;
        const [id, timer] = due[0]; timers.delete(id); timer.fn();
      }
    },
    pending() { return timers.size; },
  };
}

async function createApp(fetchImpl, {autoOpen = true, clock = null, confirmImpl = () => true} = {}) {
  const status = new Element('p');
  const cases = new Element('section');
  const refresh = new Element('button');
  const ids = {status, cases, refresh};
  for (const id of ['queue','review','filters','search','caseList','queueCount','pendingMetric','supplementMetric','completedMetric','backQueue','caseName','caseSubtitle','caseStatus','mobileCaseName','mobileDraftState','profile','limitations','validDays','sourceIntegrity','timelineTabs','timelineDays','legacySources','sourceLogs','latestReview','reviewGrid','recordsTab','highlightsTab','reportTab','draftEditor','draftPreview','draftPreviewContent','draftState','draftError','approvalState','approvalError','supplementEditor','supplementControls','supplementReadback','supplementState','supplementError','good','priority','next_7_days','comment','supplementReason','supplementRequiredContent','saveDraft','openDraftPreview','backDraftEdit','approveReview','requestMoreInfo','applySystemDraft','systemDraftHint','bottomActions']) ids[id] = new Element(id === 'search' ? 'input' : ['good','priority','next_7_days','comment','supplementReason','supplementRequiredContent'].includes(id) ? 'textarea' : id.endsWith('Tab') || ['backQueue','saveDraft','openDraftPreview','backDraftEdit','approveReview','requestMoreInfo','applySystemDraft'].includes(id) ? 'button' : 'div');
  ids.caseList = cases;
  ids.supplementControls.append(ids.supplementReason, ids.supplementRequiredContent, ids.requestMoreInfo);
  ids.supplementEditor.append(ids.supplementState, ids.supplementControls, ids.supplementError, ids.supplementReadback);
  const objectUrls = [];
  const revoked = [];
  let uuidCount = 0;
  const windowListeners = {};
  const context = {
    console,
    setTimeout: clock ? clock.setTimeout : setTimeout,
    clearTimeout: clock ? clock.clearTimeout : clearTimeout,
    AbortController,
    document: {
      getElementById: id => ids[id],
      createElement: tag => new Element(tag),
    },
    location: {href: 'https://example.test/dietitian-health-check'},
    window: {
      addEventListener(type, handler) { (windowListeners[type] ||= []).push(handler); },
      confirm: confirmImpl,
    },
    crypto: {randomUUID: () => `request-uuid-${++uuidCount}`},
    URL: {
      createObjectURL(blob) { const url = `blob:test-${objectUrls.length + 1}`; objectUrls.push({url, blob}); return url; },
      revokeObjectURL(url) { revoked.push(url); },
    },
    fetch: fetchImpl,
    liff: {
      init: async () => {},
      isLoggedIn: () => true,
      getIDToken: () => 'memory-only-token',
      login: () => { throw new Error('unexpected login'); },
    },
  };
  vm.runInNewContext(script, context, {filename: 'app.js'});
  await settle();
  if (!find(cases, el => el.tagName === 'BUTTON' && el.className === 'case-row')) {
    for (const filter of ids.filters.children) {
      await filter.dispatch('click');
      if (find(cases, el => el.tagName === 'BUTTON' && el.className === 'case-row')) break;
    }
  }
  let renderedRoot = cases;
  if (autoOpen) {
    renderedRoot = new Element('main');
    const opens = walk(cases).filter(el => el.tagName === 'BUTTON' && el.className === 'case-row');
    for (const open of opens) {
      await open.dispatch('click');
      for (const child of [...ids.timelineDays.children, ...ids.sourceLogs.children]) renderedRoot.append(child);
    }
    renderedRoot.append(ids.caseName, ids.caseSubtitle, ids.caseStatus, ids.profile, ids.limitations, ids.validDays, ids.sourceIntegrity, ids.latestReview);
  }
  return {status, cases: renderedRoot, refresh, ids, objectUrls, revoked, windowListeners, taipeiTimestamp: context.taipeiTimestamp};
}

async function draftHappyPathPostsExactFieldsAndReloads() {
  const calls = [];
  let saved = false;
  const original = {
    ...detail, updated_at: '2026-09-04T00:00:00+08:00', current_review_version: 0,
    source_token: 'a'.repeat(64),
    latest_review: null, latest_review_available: false, latest_review_fresh: null,
  };
  const fields = {good: '早餐穩定', priority: '增加蔬菜', next_7_days: '午餐加一份菜', comment: '<b>一步一步來</b>'};
  const app = await createApp(async (path, options = {}) => {
    calls.push({path, options});
    if (path.includes('?')) return jsonResponse(listing);
    if (options.method === 'POST') { saved = true; return jsonResponse({review_id: 'review-new', review_version: 1, status: 'draft', created: true}); }
    if (!saved) return jsonResponse(original);
    return jsonResponse({...original, current_review_version: 1, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 1, status: 'draft', review: fields, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'}});
  }, {autoOpen: false});
  await find(app.cases, el => el.className === 'case-row').dispatch('click');
  for (const [key, value] of Object.entries(fields)) { app.ids[key].value = value; await app.ids[key].dispatch('input'); }
  assert.ok(app.ids.draftState.textContent.includes('未儲存'));
  await app.ids.openDraftPreview.dispatch('click');
  assert.equal(app.ids.draftPreview.hidden, true, 'unsaved text cannot become the approval preview');
  assert.ok(app.ids.draftError.textContent.includes('先儲存'));
  await app.ids.saveDraft.dispatch('click');
  const post = calls.find(call => call.options.method === 'POST');
  assert.deepEqual(JSON.parse(post.options.body), {...fields, expected_source_token: original.source_token, expected_review_version: 0, request_id: 'request-uuid-1'});
  assert.equal(post.options.headers.Authorization, 'Bearer memory-only-token');
  assert.equal(post.options.credentials, 'omit');
  assert.equal(app.ids.draftState.textContent, '草稿已儲存');
  assert.equal(app.ids.good.value, fields.good, 'GET reload preserves the saved fields');
  await app.ids.openDraftPreview.dispatch('click');
  assert.equal(app.ids.draftPreview.hidden, false);
  assert.equal(app.ids.bottomActions.hidden, true, 'preview replaces the three edit actions with its own approval controls');
  assert.ok(renderedText(app.ids.draftPreviewContent).includes('<b>一步一步來</b>'), 'preview uses text, not markup');
  assert.ok(renderedText(app.ids.draftPreviewContent).includes('報告對象'), 'preview binds the visible recipient');
  assert.ok(renderedText(app.ids.draftPreviewContent).includes('目前 1 筆可驗證來源未同時綁定日期與餐別'), 'preview explains the known source binding limitation');
  assert.ok(!renderedText(app.ids.draftPreviewContent).includes('資料限制：NA'), 'preview never turns an empty review field into a fake report limitation');
  await app.ids.backDraftEdit.dispatch('click');
  assert.equal(app.ids.bottomActions.hidden, false, 'returning to edit restores the edit actions');
  assert.equal(calls.filter(call => call.path.endsWith('/case%2FA') && call.options.method !== 'POST').length, 2, 'success is accepted only after a real detail reload');
}

async function formalPreviewMatchesPersistedReportAndActualLineLiterally() {
  const contract = JSON.parse(fs.readFileSync(process.argv[7], 'utf8'));
  const savedDraft = {
    ...detail, source_token: 'a'.repeat(64), current_review_version: 9,
    latest_review_available: true, latest_review_fresh: true,
    latest_review: {
      review_version: 9, status: 'draft', review: contract.saved_review,
      ai_observations: {}, suggested_values: {}, limitations: contract.saved_limitations,
      created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00',
    },
  };
  const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(savedDraft), {autoOpen: false});
  await find(app.cases, el => el.className === 'case-row').dispatch('click');
  await app.ids.openDraftPreview.dispatch('click');
  const report = find(app.ids.draftPreviewContent, el => el.className === 'customer-report-sections');
  assert.ok(report, 'formal preview must have an independently identifiable customer-report region');
  const previewSections = report.children.map(section => ({label: section.children[0].textContent, text: section.children[1].textContent}));
  assert.deepEqual(previewSections, contract.line_sections, 'formal preview headings and values equal the actual captured LINE Flex literally');
  assert.deepEqual(contract.persisted_report, {
    contract_version: 'dietitian_health_check_report_v2',
    good: contract.saved_review.good,
    priority: contract.saved_review.priority,
    next_7_days: contract.saved_review.next_7_days,
    comment: contract.saved_review.comment,
    limitations: contract.saved_limitations,
  }, 'fixture proves the immutable SQLite report mapping');
  const reviewReference = find(app.ids.draftPreviewContent, el => el.className === 'report-review-reference');
  assert.ok(reviewReference, 'source warnings remain available as review assistance');
  assert.ok(renderedText(reviewReference).includes('審核參考，不含於送出內容'));
  assert.ok(renderedText(reviewReference).includes('未同時綁定日期與餐別'));
  assert.deepEqual(previewSections.map(section => section.label), [
    '做得好的地方', '目前優先事項', '接下來 7 天', '營養師個人點評', '資料限制',
  ]);
}

async function invalidListPayloadFailsClosedWithoutTrustedZeroes() {
  for (const [name, body] of [
    ['missing items', {}], ['null items', {items: null}], ['object items', {items: {}}],
    ['unknown status', {items: [{...listing.items[0], status: 'future_status'}]}],
    ['missing status', {items: [{...listing.items[0], status: undefined}]}],
  ]) {
    const app = await createApp(async path => path.includes('?') ? jsonResponse(body) : jsonResponse(detail), {autoOpen: false});
    assert.ok(renderedText(app.cases).includes('清單資料不可用'), `${name}: explicit unavailable state`);
    assert.equal(app.ids.pendingMetric.textContent, '資料不可用', `${name}: pending KPI is not a trusted zero`);
    assert.equal(app.ids.supplementMetric.textContent, '資料不可用', `${name}: supplement KPI is not a trusted zero`);
    assert.equal(app.ids.completedMetric.textContent, '資料不可用', `${name}: completed KPI is not a trusted zero`);
    assert.equal(walk(app.cases).filter(el => el.className === 'case-row').length, 0, `${name}: malformed list renders no actionable rows`);
    assert.ok(!renderedText(app.cases).includes('此頁目前沒有案件'), `${name}: unavailable is not empty`);
  }
  const validEmpty = await createApp(async path => path.includes('?') ? jsonResponse({items: []}) : jsonResponse(detail), {autoOpen: false});
  assert.equal(validEmpty.ids.pendingMetric.textContent, '0');
  assert.equal(validEmpty.ids.supplementMetric.textContent, '0');
  assert.equal(validEmpty.ids.completedMetric.textContent, '0');
  assert.ok(renderedText(validEmpty.cases).includes('此頁目前沒有案件'), 'only a legal empty array is a trusted zero');
}

async function draftFailureDirtyAndLateResponseMatrix() {
  const base = {...detail, updated_at: '2026-09-04T00:00:00+08:00', source_token: 'b'.repeat(64), current_review_version: 0, latest_review: null, latest_review_available: false, latest_review_fresh: null};
  const values = {good: '好', priority: '先做', next_7_days: '七天', comment: '點評'};
  {
    let postStatus = 409;
    const calls = [];
    const app = await createApp(async (path, options = {}) => {
      calls.push({path, options});
      if (path.includes('?')) return jsonResponse(listing);
      if (options.method === 'POST') return jsonResponse({}, postStatus);
      return jsonResponse(base);
    }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    for (const [key, value] of Object.entries(values)) { app.ids[key].value = value; await app.ids[key].dispatch('input'); }
    await app.ids.saveDraft.dispatch('click');
    assert.ok(app.ids.draftError.textContent.includes('重新載入'));
    assert.ok(app.ids.draftError.textContent.includes('不會自動'));
    assert.equal(app.ids.good.value, values.good, '409 preserves all fields');
    const firstBody = calls.find(call => call.options.method === 'POST').options.body;
    await app.ids.saveDraft.dispatch('click');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 1, '409 blocks same-key replay until reload');
    app.ids.comment.value = '新的點評'; await app.ids.comment.dispatch('input');
    await app.ids.saveDraft.dispatch('click');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 1, 'editing cannot hide the conflict behind a new request id');
    assert.equal(calls.find(call => call.options.method === 'POST').options.body, firstBody);
    assert.ok(app.ids.draftError.textContent.includes('重新載入'));
    const beforeUnload = {prevented: false, returnValue: undefined, preventDefault() { this.prevented = true; }};
    for (const handler of app.windowListeners.beforeunload || []) handler(beforeUnload);
    assert.equal(beforeUnload.prevented, true, 'dirty draft guards browser unload');
  }
  {
    const pending = deferred();
    const calls = [];
    const app = await createApp((path, options = {}) => {
      calls.push({path, options});
      if (path.includes('?')) return Promise.resolve(jsonResponse(listing));
      if (options.method === 'POST') return pending.promise;
      return Promise.resolve(jsonResponse(base));
    }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    for (const [key, value] of Object.entries(values)) { app.ids[key].value = value; await app.ids[key].dispatch('input'); }
    const save = app.ids.saveDraft.dispatch('click');
    await settle();
    assert.equal(app.ids.good.disabled, true, 'editing is disabled while saving');
    await app.ids.saveDraft.dispatch('click');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 1, 'duplicate save is blocked');
    await app.ids.backQueue.dispatch('click');
    pending.resolve(jsonResponse({review_version: 1, status: 'draft'}));
    await save;
    assert.equal(app.ids.queue.hidden, false);
    assert.equal(app.ids.review.hidden, true, 'late save success cannot reopen or repaint discarded case');
  }
  {
    const terminal = {...base, status: 'delivered'};
    const terminalListing = {items: [{...listing.items[0], status: 'delivered'}]};
    const calls = [];
    const app = await createApp(async (path, options = {}) => { calls.push({path, options}); return path.includes('?') ? jsonResponse(terminalListing) : jsonResponse(terminal); }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    assert.equal(app.ids.good.disabled, true);
    assert.equal(app.ids.saveDraft.disabled, true);
    assert.equal(app.ids.draftState.textContent, '此案件目前為唯讀');
    await app.ids.saveDraft.dispatch('click');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, 'terminal case never exposes a fake save path');
  }
}

async function approvalClickFlowContractAndLifecycleMatrix() {
  const fields = {good: '早餐穩定', priority: '增加蔬菜', next_7_days: '午餐加一份菜', comment: '<b>一步一步來</b>'};
  const savedDraft = {...detail, source_token: 'c'.repeat(64), current_review_version: 3, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 3, status: 'draft', review: fields, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'}};
  const approved = {...savedDraft, status: 'approved_pending_delivery', latest_review: {...savedDraft.latest_review, status: 'approved'}, approval: {report_id: apiFixture.approval.report_id, status: 'approved', delivery_status: 'pending'}};
  {
    const calls = []; const confirmations = []; let didPost = false;
    const app = await createApp(async (path, options = {}) => {
      calls.push({path, options});
      if (path.includes('?')) return jsonResponse(listing);
      if (options.method === 'POST') { didPost = true; return jsonResponse({report_id: apiFixture.approval.report_id, delivery_status: 'pending', created: true}); }
      return jsonResponse(didPost ? approved : savedDraft);
    }, {autoOpen: false, confirmImpl: message => { confirmations.push(message); return true; }});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    assert.equal(app.ids.approveReview.disabled, true, 'approval is gated until the canonical preview is visible');
    await app.ids.openDraftPreview.dispatch('click');
    assert.equal(app.ids.approveReview.disabled, false);
    assert.ok(app.ids.approvalState.textContent.includes('預覽已核對'));
    await app.ids.approveReview.dispatch('click');
    assert.ok(confirmations[0].includes('鎖定') && confirmations[0].includes('尚未送達'));
    const posts = calls.filter(call => call.options.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].path, '/api/dietitian/health-checks/case%2FA/reviews/approve');
    assert.deepEqual(JSON.parse(posts[0].options.body), {expected_source_token: savedDraft.source_token, expected_review_version: 3, request_id: 'request-uuid-1'});
    assert.equal(calls.filter(call => call.path.endsWith('/case%2FA') && call.options.method !== 'POST').length, 2);
    assert.ok(app.ids.approvalState.textContent.includes('等待投遞') && app.ids.approvalState.textContent.includes('尚未送達'));
    assert.ok(!app.ids.approvalState.textContent.includes('已送達'));
    assert.equal(app.ids.good.value, fields.good);
    assert.equal(app.ids.good.disabled, true);
  }
  {
    const calls = [];
    const app = await createApp(async (path, options = {}) => { calls.push({path, options}); return path.includes('?') ? jsonResponse(listing) : jsonResponse(savedDraft); }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    app.ids.comment.value = '尚未儲存'; await app.ids.comment.dispatch('input');
    await app.ids.approveReview.dispatch('click');
    assert.ok(app.ids.approvalError.textContent.includes('先儲存'));
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0);
  }
  {
    const calls = []; let status = 409;
    const app = await createApp(async (path, options = {}) => { calls.push({path, options}); if (path.includes('?')) return jsonResponse(listing); if (options.method === 'POST') return jsonResponse({}, status); return jsonResponse(savedDraft); }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    await app.ids.openDraftPreview.dispatch('click');
    await app.ids.approveReview.dispatch('click');
    const first = calls.find(call => call.options.method === 'POST').options.body;
    assert.ok(app.ids.approvalError.textContent.includes('人工比較'));
    assert.equal(app.ids.comment.value, fields.comment);
    status = 503; await app.ids.approveReview.dispatch('click');
    const bodies = calls.filter(call => call.options.method === 'POST').map(call => call.options.body);
    assert.equal(bodies[1], first, 'retry reuses exact request id/body and never swaps token');
  }
  {
    const clock = fakeClock(); const calls = []; let attempt = 0;
    const app = await createApp((path, options = {}) => {
      calls.push({path, options});
      if (path.includes('?')) return Promise.resolve(jsonResponse(listing));
      if (options.method === 'POST') { attempt += 1; return attempt === 1 ? new Promise(() => {}) : Promise.resolve(jsonResponse({}, 503)); }
      return Promise.resolve(jsonResponse(savedDraft));
    }, {autoOpen: false, clock});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    await app.ids.openDraftPreview.dispatch('click');
    const firstClick = app.ids.approveReview.dispatch('click'); await settle();
    clock.advance(15000); await firstClick;
    assert.ok(app.ids.approvalError.textContent.includes('同一請求重試'));
    const firstBody = calls.find(call => call.options.method === 'POST').options.body;
    await app.ids.approveReview.dispatch('click');
    const postBodies = calls.filter(call => call.options.method === 'POST').map(call => call.options.body);
    assert.equal(postBodies[1], firstBody, 'timeout retry retains the identical request id/body');
  }
  {
    const pending = deferred(); const calls = [];
    const two = {items: [listing.items[0], {...listing.items[0], case_id: 'case-B'}]};
    const app = await createApp((path, options = {}) => { calls.push({path, options}); if (path.includes('?')) return Promise.resolve(jsonResponse(two)); if (options.method === 'POST') return pending.promise; return Promise.resolve(jsonResponse({...savedDraft, case_id: path.endsWith('/case-B') ? 'case-B' : 'case/A'})); }, {autoOpen: false});
    let rows = walk(app.cases).filter(el => el.className === 'case-row'); await rows[0].dispatch('click');
    await app.ids.openDraftPreview.dispatch('click');
    const approval = app.ids.approveReview.dispatch('click'); await settle();
    assert.equal(app.ids.approveReview.disabled, true);
    await app.ids.approveReview.dispatch('click');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 1);
    await app.ids.backQueue.dispatch('click'); rows = walk(app.cases).filter(el => el.className === 'case-row'); await rows[1].dispatch('click');
    pending.resolve(jsonResponse({report_id: 'late-report', delivery_status: 'pending', created: true})); await approval;
    assert.ok(!app.ids.approvalState.textContent.includes('late-report'));
  }
}

async function outcomeUnknownRequiresManualReconciliationWithoutResend() {
  const unknownFixture = JSON.parse(fs.readFileSync(process.argv[8], 'utf8'));
  assert.equal(unknownFixture.status, 'approved_pending_delivery');
  assert.equal(unknownFixture.approval.delivery_status, 'outcome_unknown');
  const unknownListing = {items: [{
    case_id: unknownFixture.case_id, status: unknownFixture.status,
    valid_day_count: unknownFixture.valid_day_count,
    window_started_at: unknownFixture.window_started_at,
    window_ends_at: unknownFixture.window_ends_at,
  }]};
  const calls = [];
  const app = await createApp(async (path, options = {}) => {
    calls.push({path, options});
    return path.includes('?') ? jsonResponse(unknownListing) : jsonResponse(unknownFixture);
  }, {autoOpen: false});
  await settle();
  const queueText = renderedText(app.cases);
  assert.ok(queueText.includes('投遞結果未知'));
  assert.ok(queueText.includes('需人工核實 LINE 實際收件結果'));
  const row = find(app.cases, el => el.className === 'case-row');
  await row.dispatch('click');
  assert.equal(app.ids.caseStatus.textContent, '待投遞');
  assert.ok(app.ids.approvalState.textContent.includes('投遞結果未知'));
  assert.ok(app.ids.approvalState.textContent.includes('人工核實'));
  assert.ok(app.ids.approvalState.textContent.includes('不會自動重送'));
  assert.ok(!app.ids.approvalState.textContent.includes('已送達'));
  for (const key of ['good','priority','next_7_days','comment']) assert.equal(app.ids[key].disabled, true, `${key} stays immutable`);
  assert.equal(app.ids.saveDraft.disabled, true);
  assert.equal(app.ids.approveReview.disabled, true);
  assert.equal(walk(app.cases).filter(el => el.tagName === 'BUTTON' && el.textContent.includes('重送')).length, 0);
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, 'unknown rendering is GET-only');
}

async function approvalReadbackProjectionMismatchMatrixFailsClosed() {
  const fields = {good: '早餐穩定', priority: '增加蔬菜', next_7_days: '午餐加一份菜', comment: '一步一步來'};
  const savedDraft = {...detail, source_token: 'd'.repeat(64), current_review_version: 4, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 4, status: 'draft', review: fields, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'}};
  const reportId = apiFixture.approval.report_id;
  const validReadback = {...savedDraft, status: 'approved_pending_delivery', latest_review: {...savedDraft.latest_review, status: 'approved'}, approval: {report_id: reportId, status: 'approved', delivery_status: 'pending'}};
  const invalidReadbacks = [
    ['wrong report_id', {...validReadback, approval: {...validReadback.approval, report_id: `${reportId}-wrong`}}],
    ['latest review not approved', {...validReadback, latest_review: {...validReadback.latest_review, status: 'draft'}}],
    ['approval not approved', {...validReadback, approval: {...validReadback.approval, status: 'draft'}}],
    ['delivery not pending', {...validReadback, approval: {...validReadback.approval, delivery_status: 'delivered'}}],
    ['case not approved_pending_delivery', {...validReadback, status: 'ready_for_review'}],
  ];
  for (const [name, invalidReadback] of invalidReadbacks) {
    let didPost = false;
    const app = await createApp(async (path, options = {}) => {
      if (path.includes('?')) return jsonResponse(listing);
      if (options.method === 'POST') { didPost = true; return jsonResponse({report_id: reportId, delivery_status: 'pending', created: true}); }
      return jsonResponse(didPost ? invalidReadback : savedDraft);
    }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    await app.ids.openDraftPreview.dispatch('click');
    await app.ids.approveReview.dispatch('click');
    assert.ok(app.ids.approvalError.textContent.includes('結果未知'), `${name}: contradictory readback must fail closed`);
    assert.ok(!app.ids.approvalState.textContent.includes('等待投遞'), `${name}: contradictory readback must not display approval success`);
    assert.equal(app.ids.good.disabled, false, `${name}: failed confirmation must retain the editable saved draft`);
  }
}

async function approvalLateCompletionAfterAppGenerationRefreshCannotPaint() {
  const fields = {good: '好', priority: '先做', next_7_days: '七天', comment: '點評'};
  const savedDraft = {...detail, source_token: 'e'.repeat(64), current_review_version: 5, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 5, status: 'draft', review: fields, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'}};
  const approved = {...savedDraft, status: 'approved_pending_delivery', latest_review: {...savedDraft.latest_review, status: 'approved'}, approval: {report_id: apiFixture.approval.report_id, status: 'approved', delivery_status: 'pending'}};
  {
    const pendingPost = deferred(); let listCalls = 0;
    const app = await createApp((path, options = {}) => {
      if (path.includes('?')) { listCalls += 1; return Promise.resolve(jsonResponse(listCalls === 1 ? listing : {items: []})); }
      if (options.method === 'POST') return pendingPost.promise;
      return Promise.resolve(jsonResponse(savedDraft));
    }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    await app.ids.openDraftPreview.dispatch('click');
    const approval = app.ids.approveReview.dispatch('click'); await settle();
    await app.refresh.dispatch('click');
    pendingPost.resolve(jsonResponse({report_id: apiFixture.approval.report_id, delivery_status: 'pending', created: true}));
    await approval;
    assert.equal(app.ids.queue.hidden, false, 'late approval POST must not reopen the old detail after refresh');
    assert.equal(app.ids.review.hidden, true, 'late approval POST must remain fenced by app generation');
    assert.ok(!app.ids.approvalState.textContent.includes('等待投遞'), 'late approval POST must not paint success into the refreshed snapshot');
  }
  {
    const pendingReadback = deferred(); let listCalls = 0; let detailCalls = 0;
    const app = await createApp((path, options = {}) => {
      if (path.includes('?')) { listCalls += 1; return Promise.resolve(jsonResponse(listCalls === 1 ? listing : {items: []})); }
      if (options.method === 'POST') return Promise.resolve(jsonResponse({report_id: apiFixture.approval.report_id, delivery_status: 'pending', created: true}));
      detailCalls += 1;
      return detailCalls === 1 ? Promise.resolve(jsonResponse(savedDraft)) : pendingReadback.promise;
    }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    await app.ids.openDraftPreview.dispatch('click');
    const approval = app.ids.approveReview.dispatch('click'); await settle();
    await app.refresh.dispatch('click');
    pendingReadback.resolve(jsonResponse(approved));
    await approval;
    assert.equal(app.ids.queue.hidden, false, 'late approval GET must not reopen the old detail after refresh');
    assert.equal(app.ids.review.hidden, true, 'late approval GET must remain fenced by app generation');
    assert.ok(!app.ids.approvalState.textContent.includes('等待投遞'), 'late approval GET must not paint success into the refreshed snapshot');
  }
}

async function terminalApprovalClickNeverPosts() {
  const fields = {good: '好', priority: '先做', next_7_days: '七天', comment: '點評'};
  const terminal = {...detail, status: 'delivered', source_token: 'f'.repeat(64), current_review_version: 6, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 6, status: 'approved', review: fields, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'}};
  const terminalListing = {items: [{...listing.items[0], status: 'delivered'}]};
  const calls = [];
  const app = await createApp(async (path, options = {}) => { calls.push({path, options}); return path.includes('?') ? jsonResponse(terminalListing) : jsonResponse(terminal); }, {autoOpen: false});
  await find(app.cases, el => el.className === 'case-row').dispatch('click');
  assert.equal(app.ids.approveReview.disabled, true);
  await app.ids.approveReview.dispatch('click');
  assert.equal(calls.filter(call => call.options.method === 'POST' && call.path.endsWith('/reviews/approve')).length, 0, 'terminal approval click must never POST');
}

async function cancelledApprovalConfirmationNeverPosts() {
  const fields = {good: '好', priority: '先做', next_7_days: '七天', comment: '保留內容'};
  const savedDraft = {...detail, source_token: '1'.repeat(64), current_review_version: 7, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 7, status: 'draft', review: fields, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'}};
  const calls = []; const confirmations = [];
  const app = await createApp(async (path, options = {}) => { calls.push({path, options}); return path.includes('?') ? jsonResponse(listing) : jsonResponse(savedDraft); }, {autoOpen: false, confirmImpl: message => { confirmations.push(message); return false; }});
  await find(app.cases, el => el.className === 'case-row').dispatch('click');
  await app.ids.openDraftPreview.dispatch('click');
  await app.ids.approveReview.dispatch('click');
  assert.equal(confirmations.length, 1, 'approval asks exactly one confirmation');
  assert.equal(calls.filter(call => call.options.method === 'POST' && call.path.endsWith('/reviews/approve')).length, 0, 'cancelled confirmation must never POST');
  assert.equal(app.ids.comment.value, fields.comment, 'cancelled confirmation retains the saved draft');
  assert.ok(app.ids.approvalState.textContent.includes('預覽已核對'));
}

async function approvalReadbackRendersUntrustedTextLiterally() {
  const attacks = {
    good: '<img src=x onerror="globalThis.injected=1">',
    priority: '<script>globalThis.injected=2</script>',
    next_7_days: '<b>七天行動</b>',
    comment: '<svg onload="globalThis.injected=3"></svg>',
  };
  const savedDraft = {...detail, source_token: '2'.repeat(64), current_review_version: 8, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 8, status: 'draft', review: attacks, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'}};
  const approved = {...savedDraft, status: 'approved_pending_delivery', latest_review: {...savedDraft.latest_review, status: 'approved'}, approval: {report_id: apiFixture.approval.report_id, status: 'approved', delivery_status: 'pending'}};
  let didPost = false;
  const app = await createApp(async (path, options = {}) => {
    if (path.includes('?')) return jsonResponse(listing);
    if (options.method === 'POST') { didPost = true; return jsonResponse({report_id: apiFixture.approval.report_id, delivery_status: 'pending', created: true}); }
    return jsonResponse(didPost ? approved : savedDraft);
  }, {autoOpen: false});
  await find(app.cases, el => el.className === 'case-row').dispatch('click');
  await app.ids.openDraftPreview.dispatch('click');
  await app.ids.approveReview.dispatch('click');
  const readbackText = renderedText(app.ids.latestReview);
  for (const [key, literal] of Object.entries(attacks)) {
    assert.equal(app.ids[key].value, literal, `human draft remains literal in its own editor: ${key}`);
    assert.ok(!readbackText.includes(literal), `AI summary must not absorb human draft: ${key}`);
  }
  assert.equal(walk(app.ids.latestReview).filter(el => ['IMG','SCRIPT','B','SVG'].includes(el.tagName)).length, 0, 'AI readback must not create elements from untrusted review text');
  assert.ok(app.ids.approvalState.textContent.includes('等待投遞'));
}

async function missingOrMalformedSourceTokenKeepsDraftReadOnly() {
  for (const source_token of [undefined, '', 'not-a-token', 'a'.repeat(63), 'G'.repeat(64)]) {
    const body = {...detail, source_token, current_review_version: 0, latest_review: null, latest_review_available: false, latest_review_fresh: null};
    const calls = [];
    const app = await createApp(async (path, options = {}) => { calls.push({path, options}); return path.includes('?') ? jsonResponse(listing) : jsonResponse(body); }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    assert.equal(app.ids.saveDraft.disabled, true, `invalid source token must be read-only: ${source_token}`);
    assert.equal(app.ids.draftState.textContent, '此案件目前為唯讀');
    for (const key of ['good','priority','next_7_days','comment']) app.ids[key].value = '不可送出';
    await app.ids.saveDraft.dispatch('click');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0);
  }
}

async function queueRowsHaveOneCtaAndFourGridItems() {
  const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(detail), {autoOpen: false});
  const row = find(app.cases, el => el.className === 'case-row');
  assert.equal(row.children.length, 4, 'four-column row must have exactly four grid items');
  assert.equal(row.textContent, '', 'row must not retain an anonymous CTA text node');
  assert.equal(walk(row).filter(el => el.textContent === '開啟案件 →').length, 1, 'each row exposes exactly one CTA');
}

async function verifiedApiReviewProjectionIsRenderedInChinese() {
  const fixtureListing = {items: [{...listing.items[0], case_id: apiFixture.case_id, status: apiFixture.status}]};
  const app = await createApp(async path => path.includes('?') ? jsonResponse(fixtureListing) : jsonResponse(apiFixture));
  const text = renderedText(app.ids.latestReview);
  for (const expected of ['資料來源：6 筆可驗證營養快照', '營養計算', '整體重點：資料不足']) assert.ok(text.includes(expected), expected);
  for (const forbidden of ['記錄完整', '增加蔬菜', '午餐補一份蔬菜', '營養師個人點評', '審核狀態', '審核版本', 'review-2', apiFixture.source_manifest_hash, '/srv/', 'activation-secret']) {
    if (forbidden) assert.ok(!text.includes(forbidden), `AI summary must not render human draft or technical metadata: ${forbidden}`);
  }

  const stale = {...apiFixture, latest_review_fresh: false, latest_review_available: false};
  const staleApp = await createApp(async path => path.includes('?') ? jsonResponse(fixtureListing) : jsonResponse(stale));
  assert.ok(renderedText(staleApp.ids.latestReview).includes('整體重點：資料不足'));

  const malformed = {...apiFixture, latest_review: {...apiFixture.latest_review, review: {strengths: {raw: 'secret-path'}}, ai_observations: []}};
  const malformedApp = await createApp(async path => path.includes('?') ? jsonResponse(fixtureListing) : jsonResponse(malformed));
  const malformedText = renderedText(malformedApp.ids.latestReview);
  assert.ok(malformedText.includes('整體重點：資料不足'));
  assert.ok(!malformedText.includes('secret-path'));
}

async function queueWaitingCopyMatchesDeliveryState() {
  const queueListing = {items: [
    {case_id: 'ready', status: 'ready_for_review', valid_day_count: 3, submitted_at: '2026-09-04T01:00:00Z'},
    {case_id: 'pending', status: 'approved_pending_delivery', valid_day_count: 3, submitted_at: null},
    {case_id: 'failed', status: 'delivery_failed', valid_day_count: 3, submitted_at: '2026-09-04T02:00:00Z'},
    {case_id: 'delivered', status: 'delivered', valid_day_count: 3, submitted_at: '2026-09-04T03:00:00Z'},
    {case_id: 'cancelled', status: 'cancelled', valid_day_count: 3, submitted_at: null},
  ]};
  const app = await createApp(async path => path.includes('?') ? jsonResponse(queueListing) : jsonResponse(detail), {autoOpen: false});
  const copy = {};
  copy.ready = renderedText(walk(app.cases).find(el => el.className === 'case-row'));
  const completedTab = app.ids.filters.children.find(button => button.textContent === '已完成');
  assert.ok(completedTab, 'completed lifecycle group remains reachable');
  await completedTab.dispatch('click');
  const completedRows = walk(app.cases).filter(el => el.className === 'case-row');
  for (const [index, id] of ['pending', 'failed', 'delivered', 'cancelled'].entries()) copy[id] = renderedText(completedRows[index]);
  assert.ok(copy.ready.includes('已送出，等待處理｜2026/09/04 09:00'));
  assert.ok(copy.pending.includes('等待投遞｜尚無後端送出時間'));
  assert.ok(copy.failed.includes('投遞失敗｜送出時間：2026/09/04 10:00'));
  assert.ok(copy.delivered.includes('案件已送達｜送出時間：2026/09/04 11:00'));
  assert.ok(!copy.delivered.includes('等待'));
  assert.ok(copy.cancelled.includes('案件已取消｜尚無後端送出時間'));
  assert.ok(!copy.cancelled.includes('2026/'), 'missing submitted_at must not fabricate a timestamp');
}

async function liveQueueAndDetailNavigation() {
  const queueListing = {items: [
    {...listing.items[0], profile: {name: '王小明', goal: '減脂'}, submitted_at: '2026-09-04T01:00:00Z'},
    {case_id: 'case-B', status: 'approved_pending_delivery', valid_day_count: 3, profile: {name: null, goal: null}, submitted_at: null},
    {case_id: 'case-C', status: 'delivered', valid_day_count: 3, profile: {name: '林小姐', goal: '維持'}, submitted_at: '2026-09-03T01:00:00Z'},
  ]};
  const calls = [];
  const app = await createApp(async path => {
    calls.push(path);
    if (path.includes('?')) return jsonResponse(queueListing);
    if (path.endsWith('/case%2FA')) return jsonResponse({...detail, profile: {name: '王小明', goal: '減脂', restrictions: '花生', tdee: 2100, protein: 100, active_days: '一、三、五'}, latest_review: null, latest_review_fresh: false});
    return jsonResponse(detail, 503);
  }, {autoOpen: false});
  let text = renderedText(app.cases);
  assert.ok(text.includes('王小明'));
  assert.ok(text.includes('減脂'));
  assert.ok(!text.includes('共 3 案'), 'limited page must not claim a global count');
  assert.ok(text.includes('已送出，等待處理'));
  const completedTab = app.ids.filters.children.find(button => button.textContent === '已完成');
  await completedTab.dispatch('click');
  const completedText = renderedText(app.cases);
  assert.ok(completedText.includes('案件代碼待同步'));
  assert.ok(!completedText.includes('未提供'), 'missing profile no longer makes identical card titles');
  assert.ok(completedText.includes('待投遞'));
  assert.ok(completedText.includes('已送達'));
  const reviewTab = app.ids.filters.children.find(button => button.textContent === '待審核');
  await reviewTab.dispatch('click');

  app.ids.search.value = '王';
  await app.ids.search.dispatch('input');
  assert.equal(walk(app.cases).filter(el => el.className === 'case-row').length, 1);
  app.ids.search.value = '';
  await app.ids.search.dispatch('input');
  const open = find(app.cases, el => el.tagName === 'BUTTON' && el.className === 'case-row');
  await open.dispatch('click');
  assert.equal(app.ids.queue.hidden, true);
  assert.equal(app.ids.review.hidden, false);
  const detailText = [renderedText(app.ids.profile), renderedText(app.ids.limitations), renderedText(app.ids.validDays), renderedText(app.ids.sourceIntegrity), renderedText(app.ids.latestReview)].join('\n');
  assert.ok(detailText.includes('王小明'));
  assert.ok(detailText.includes('花生'));
  assert.ok(detailText.includes('尚無可驗證的系統觀察'));
  assert.ok(!detailText.includes('2099-12-31'));
  await app.ids.backQueue.dispatch('click');
  assert.equal(app.ids.queue.hidden, false);
  assert.equal(app.ids.review.hidden, true);

  assert.ok(calls.includes('/api/dietitian/health-checks?limit=25&offset=0'));
  assert.ok(calls.includes('/api/dietitian/health-checks/case%2FA'));
}

async function oneBrokenDetailDoesNotEraseQueue() {
  const two = {items: [listing.items[0], {...listing.items[0], case_id: 'broken'}]};
  const app = await createApp(async path => path.includes('?') ? jsonResponse(two) : path.endsWith('/broken') ? jsonResponse({}, 503) : jsonResponse({...detail, profile: {name: '不應殘留'}}), {autoOpen: false});
  const rows = walk(app.cases).filter(el => el.className === 'case-row');
  assert.equal(rows.length, 2);
  await rows[0].dispatch('click');
  assert.ok(renderedText(app.ids.profile).includes('不應殘留'));
  await app.ids.backQueue.dispatch('click');
  await rows[1].dispatch('click');
  assert.ok(renderedText(app.ids.latestReview).includes('讀取失敗'));
  assert.ok(!renderedText(app.ids.profile).includes('不應殘留'), 'failed detail must clear the prior case evidence');
  assert.equal(renderedText(app.ids.timelineDays), '', 'failed detail must clear the prior timeline');
  assert.equal(renderedText(app.ids.sourceLogs), '', 'failed detail must clear legacy sources');
  assert.equal(walk(app.cases).filter(el => el.className === 'case-row').length, 2, 'detail failure keeps both queue rows');
}

async function newerDetailStatusControlsDetailAndPhotos() {
  const delivered = {...detail, case_id: 'case/A', status: 'delivered'};
  const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(delivered));
  assert.equal(app.ids.caseStatus.textContent, '已送達');
  assert.ok(renderedText(app.ids.sourceLogs).includes('案件已送達，照片不再開放'));
  assert.equal(find(app.ids.sourceLogs, el => el.tagName === 'BUTTON' && el.textContent === '查看照片'), undefined);
}

async function hungDetailCannotBlockQueueOrHealthyCase() {
  const hung = deferred();
  const two = {items: [
    {...listing.items[0], case_id: 'healthy'},
    {...listing.items[0], case_id: 'hung'},
  ]};
  const app = await createApp(path => {
    if (path.includes('?')) return Promise.resolve(jsonResponse(two));
    if (path.endsWith('/healthy')) return Promise.resolve(jsonResponse({...detail, case_id: 'healthy'}));
    return hung.promise;
  }, {autoOpen: false});
  const rows = walk(app.cases).filter(el => el.className === 'case-row');
  assert.equal(rows.length, 2, 'list rows render without waiting for every detail');
  await rows[0].dispatch('click');
  assert.ok(renderedText(app.ids.sourceLogs).includes('餐點紀錄 1'), 'healthy detail remains usable');
  for (const handler of app.windowListeners.pagehide || []) handler();
}

async function boundedFetchAndJsonTimeoutsUseAbortAndClearTimers() {
  for (const phase of ['fetch', 'json']) {
    const clock = fakeClock();
    const calls = [];
    const app = await createApp((path, options) => {
      calls.push({path, options});
      if (path.includes('?')) return Promise.resolve(jsonResponse(listing));
      if (phase === 'fetch') return new Promise(() => {});
      return Promise.resolve({ok: true, status: 200, json: () => new Promise(() => {})});
    }, {autoOpen: false, clock});
    assert.equal(walk(app.cases).filter(el => el.className === 'case-row').length, 1, `${phase}: queue is already usable`);
    clock.advance(15000);
    await settle();
    const detailCall = calls.find(call => !call.path.includes('?'));
    assert.equal(detailCall.options.signal.aborted, true, `${phase}: timeout aborts transport`);
    const row = find(app.cases, el => el.className === 'case-row');
    await row.dispatch('click');
    assert.ok(renderedText(app.ids.latestReview).includes('讀取逾時，請重試'), `${phase}: case-local timeout is visible`);
    assert.equal(clock.pending(), 0, `${phase}: timer is cleared`);
  }
}

async function reverseReloadCannotOverwriteNewQueue() {
  const oldList = deferred();
  let listCalls = 0;
  const app = await createApp(path => {
    if (path.includes('?')) {
      listCalls += 1;
      return listCalls === 1 ? oldList.promise : Promise.resolve(jsonResponse({items: [{...listing.items[0], case_id: 'new', profile: {name: '新清單'}}]}));
    }
    return Promise.resolve(jsonResponse({...detail, case_id: 'new', profile: {name: '新清單'}}));
  }, {autoOpen: false});
  await app.refresh.dispatch('click');
  oldList.resolve(jsonResponse({items: [{...listing.items[0], case_id: 'old', profile: {name: '舊清單'}}]}));
  await settle();
  assert.ok(renderedText(app.cases).includes('新清單'));
  assert.ok(!renderedText(app.cases).includes('舊清單'));
}

async function reverseReloadDetailCannotOverwriteNewSnapshot() {
  const oldDetail = deferred();
  let listCalls = 0;
  const app = await createApp(path => {
    if (path.includes('?')) {
      listCalls += 1;
      return Promise.resolve(jsonResponse({items: [{...listing.items[0], profile: {name: listCalls === 1 ? '舊列' : '新列'}}]}));
    }
    return listCalls === 1 ? oldDetail.promise : Promise.resolve(jsonResponse({...detail, status: 'delivered', profile: {name: '新明細'}}));
  }, {autoOpen: false});
  await app.refresh.dispatch('click');
  oldDetail.resolve(jsonResponse({...detail, status: 'ready_for_review', profile: {name: '舊明細'}}));
  await settle();
  const row = find(app.cases, el => el.className === 'case-row');
  assert.ok(renderedText(row).includes('已送達'), 'new detail status hydrates queue');
  await row.dispatch('click');
  assert.equal(app.ids.caseName.textContent, '新明細');
  assert.equal(app.ids.caseStatus.textContent, '已送達');
}

async function lateDetailCannotOverwriteAnotherOpenCase() {
  const pendingA = deferred();
  const pendingB = deferred();
  const two = {items: [
    {...listing.items[0], case_id: 'case-A', profile: {name: '列表 A'}},
    {...listing.items[0], case_id: 'case-B', profile: {name: '列表 B'}},
  ]};
  const app = await createApp(path => path.includes('?') ? Promise.resolve(jsonResponse(two)) : path.endsWith('/case-A') ? pendingA.promise : pendingB.promise, {autoOpen: false});
  let rows = walk(app.cases).filter(el => el.className === 'case-row');
  await rows[0].dispatch('click');
  await app.ids.backQueue.dispatch('click');
  rows = walk(app.cases).filter(el => el.className === 'case-row');
  await rows[1].dispatch('click');
  pendingA.resolve(jsonResponse({...detail, case_id: 'case-A', profile: {name: '明細 A'}}));
  await settle();
  assert.ok(!app.ids.caseName.textContent.includes('明細 A'), 'late A cannot paint over active B');
  pendingB.resolve(jsonResponse({...detail, case_id: 'case-B', profile: {name: '明細 B'}}));
  await settle();
  assert.equal(app.ids.caseName.textContent, '明細 B');
}

async function invalidDetailIdentityOrStatusFailsClosed() {
  for (const [name, invalid] of [
    ['missing case', {...detail, case_id: undefined}],
    ['mismatched case', {...detail, case_id: 'other'}],
    ['unknown status', {...detail, status: 'future_status'}],
    ['missing status', {...detail, status: undefined}],
  ]) {
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(invalid), {autoOpen: false});
    const row = find(app.cases, el => el.className === 'case-row');
    await row.dispatch('click');
    assert.ok(renderedText(app.ids.latestReview).includes('不可用') || renderedText(app.ids.latestReview).includes('不一致'), name);
    assert.equal(find(app.ids.sourceLogs, el => el.textContent === '查看照片'), undefined, `${name}: no permission fallback`);
  }
}

async function happyPath() {
  const calls = [];
  const app = await createApp(async (path, options) => {
    calls.push({path, options});
    if (path.includes('/sources/')) return imageResponse();
    if (path.includes('?')) return jsonResponse(listing);
    return jsonResponse(detail);
  });
  assert.equal(calls.filter(call => call.path.includes('/sources/')).length, 0, 'images must not eagerly load');
  const text = renderedText(app.cases);
  assert.ok(text.includes('約680 kcal（580～800）'));
  assert.ok(text.includes('蛋白質約35g（29～43）'));
  assert.ok(text.includes('脂肪：未提供'));
  assert.ok(text.includes('碳水化合物：未提供'));
  assert.ok(text.includes('餐點紀錄'));
  assert.ok(text.includes('日期／餐別尚無可驗證資料'));
  assert.ok(text.includes('膳食纖維：未提供'));
  assert.ok(text.includes('鈉：未提供'));
  assert.ok(text.includes('非營養師核准'));
  assert.ok(!text.includes('脂肪：NA'));
  assert.ok(!text.includes('碳水化合物：NA'));
  assert.ok(!text.includes('膳食纖維：NA'));
  assert.ok(!text.includes('鈉：NA'));
  assert.ok(!text.includes('熱量：NA'));
  const view = find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片');
  assert.ok(view, 'source has a view-photo button');
  await view.dispatch('click');
  const imageCall = calls.find(call => call.path.includes('/sources/'));
  assert.equal(imageCall.path, '/api/dietitian/health-checks/case%2FA/sources/log%20B/image');
  assert.equal(imageCall.options.headers.Authorization, 'Bearer memory-only-token');
  assert.equal(imageCall.options.credentials, 'omit');
  assert.equal(imageCall.options.cache, 'no-store');
  assert.ok(imageCall.options.signal instanceof AbortSignal);
  assert.equal(app.objectUrls.length, 1);
  const image = find(app.cases, el => el.tagName === 'IMG');
  assert.equal(image.attributes.src, 'blob:test-1');
  assert.equal(image.attributes.alt, '餐點照片');
  assert.ok(image.attributes['aria-label'].includes('Enter') && image.attributes['aria-label'].includes('空白鍵'));
  await image.dispatch('click');
  assert.equal(image.className, 'expanded');
  let prevented = 0;
  await image.dispatch('keydown', {key: 'Enter', preventDefault() { prevented += 1; }});
  assert.equal(image.className, '', 'Enter toggles exactly once');
  await image.dispatch('keydown', {key: ' ', preventDefault() { prevented += 1; }});
  assert.equal(image.className, 'expanded', 'Space toggles exactly once');
  await image.dispatch('keydown', {key: 'Escape', preventDefault() { prevented += 1; }});
  assert.equal(image.className, 'expanded', 'other keys do not toggle');
  assert.equal(prevented, 2, 'Enter and Space prevent default scrolling/synthetic activation');

  const close = find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '關閉照片');
  await close.dispatch('click');
  assert.deepEqual(app.revoked, ['blob:test-1']);
  assert.equal(find(app.cases, el => el.tagName === 'IMG'), undefined);
}

async function zeroNutritionValuesRemainRecordedValues() {
  const zeroDetail = {
    ...detail,
    source_logs: [{
      ...detail.source_logs[0],
      nutrition_snapshot: {
        calories_kcal: 0, protein_g: 0, fat_g: 0,
        carbohydrate_g: 0, fiber_g: 0, sodium_mg: 0,
      },
    }],
  };
  const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(zeroDetail));
  const text = renderedText(app.cases);
  for (const expected of ['脂肪：0 g', '碳水化合物：0 g', '膳食纖維：0 g', '鈉：0 mg']) {
    assert.ok(text.includes(expected), `legal zero remains present: ${expected}`);
  }
}

async function verifiedTimelineGroupsDatesSlotsAndKeepsLegacy() {
  const logs = [
    {log_id: 'd3', food_log_version: 1, local_date: '2026-09-05', normalized_meal_slot: '晚餐', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 530}},
    {log_id: 'd1b', food_log_version: 1, local_date: '2026-09-02', normalized_meal_slot: '早餐', trust_type: 'verified_snapshot', nutrition_snapshot: {protein_g: 22}},
    {log_id: 'legacy', food_log_version: 1, trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 400}},
    {log_id: 'd1u', food_log_version: 1, local_date: '2026-09-02', normalized_meal_slot: 'unspecified', trust_type: 'verified_snapshot', nutrition_snapshot: {fiber_g: 8}},
    {log_id: 'd2', food_log_version: 1, local_date: '2026-09-03', normalized_meal_slot: '午餐', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 610}},
    {log_id: 'd4', food_log_version: 1, local_date: '2026-09-06', normalized_meal_slot: '點心', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 180}},
    {log_id: 'bad-date', food_log_version: 1, local_date: '2026-02-30', normalized_meal_slot: '早餐', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 999}},
    {log_id: 'missing-slot', food_log_version: 1, local_date: '2026-09-04', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 777}},
    {log_id: 'd1b', food_log_version: 1, local_date: '2026-09-02', normalized_meal_slot: '早餐', trust_type: 'verified_snapshot', nutrition_snapshot: {protein_g: 22}},
  ];
  const timeline = {...detail, source_logs: logs, source_integrity: {referenced_count: 9, available_snapshot_count: 9, all_snapshots_available: true}};
  const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(timeline));
  const tabs = walk(app.ids.timelineTabs).filter(el => el.tagName === 'BUTTON');
  assert.deepEqual(tabs.map(tab => tab.textContent), ['第1天・09/02', '第2天・09/03', '第3天・09/05', '第4天・09/06']);
  let dayText = renderedText(app.ids.timelineDays);
  assert.ok(dayText.includes('早餐'));
  assert.ok(dayText.includes('餐別未提供'));
  assert.ok(dayText.includes('蛋白質：22 g'));
  assert.equal(walk(app.ids.timelineDays).filter(el => el.className === 'meal-card').length, 2, 'duplicate log is shown once');
  await tabs[3].dispatch('click');
  dayText = renderedText(app.ids.timelineDays);
  assert.ok(dayText.includes('點心'));
  assert.ok(dayText.includes('熱量：180 kcal'));
  assert.ok(!dayText.includes('蛋白質：22 g'));
  const legacyText = renderedText(app.ids.sourceLogs);
  assert.ok(legacyText.includes('日期／餐別尚無可驗證資料'));
  assert.ok(legacyText.includes('熱量：400 kcal'));
  assert.ok(legacyText.includes('熱量：999 kcal'));
  assert.ok(legacyText.includes('熱量：777 kcal'));
  assert.ok(!renderedText(app.ids.timelineTabs).includes('09/04'), 'single missing binding field does not borrow a date');
}

async function mealSlotsUseCanonicalPresentationOrderWithoutDroppingUnknowns() {
  const slots = [
    ['unknown-zh', '宵夜'], ['snack-zh', '點心'], ['prototype-constructor', 'constructor'],
    ['lunch-en', 'lunch'], ['prototype-to-string', 'toString'], ['breakfast-zh', '早餐'],
    ['prototype-has-own', 'hasOwnProperty'], ['missing', 'unspecified'], ['dinner-en', 'dinner'],
    ['prototype-value-of', 'valueOf'], ['unknown-ascii', 'Alpha-special'], ['snack-en', 'snack'],
    ['prototype-locale', 'toLocaleString'], ['lunch-zh', '午餐'], ['prototype-proto', '__proto__'],
    ['breakfast-en', 'breakfast'], ['dinner-zh', '晚餐'],
  ];
  const logs = slots.map(([log_id, normalized_meal_slot]) => ({
    log_id, food_log_version: 1, local_date: '2026-09-02', normalized_meal_slot,
    trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 100},
  }));
  const body = {...detail, source_logs: logs, source_integrity: {referenced_count: logs.length, available_snapshot_count: logs.length, all_snapshots_available: true}};
  const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(body));
  const headings = walk(app.ids.timelineDays).filter(el => el.tagName === 'H3').map(el => el.textContent);
  assert.deepEqual(headings, [
    '早餐', '早餐', '午餐', '午餐', '晚餐', '晚餐', '點心', '點心',
    'Alpha-special', '__proto__', 'constructor', 'hasOwnProperty',
    'toLocaleString', 'toString', 'valueOf', '宵夜', '餐別未提供',
  ]);
  assert.equal(walk(app.ids.timelineDays).filter(el => el.className === 'meal-card').length, logs.length, 'known, unknown, and legacy-compatible slots retain separate source identities');
}

async function duplicateIdentityPrefersWholeBoundSourceRegardlessOfOrder() {
  const bound = {log_id: 'same', food_log_version: 7, local_date: '2026-09-02', normalized_meal_slot: '早餐', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 222, protein_g: 22}};
  const legacy = {log_id: 'same', food_log_version: 7, trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 111, protein_g: 11}};
  const orders = [
    ['legacy first', [legacy, bound, {...bound}]],
    ['legacy middle', [bound, legacy, {...bound}]],
    ['legacy last', [bound, {...bound}, legacy]],
  ];
  for (const [name, logs] of orders) {
    const body = {...detail, source_logs: logs, source_integrity: {referenced_count: 3, available_snapshot_count: 3, all_snapshots_available: true}};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(body));
    const text = renderedText(app.cases);
    assert.ok(renderedText(app.ids.timelineTabs).includes('第1天・09/02'), `${name}: bound source remains on timeline`);
    assert.ok(text.includes('熱量：222 kcal'), `${name}: whole bound source nutrition is retained`);
    assert.ok(text.includes('蛋白質：22 g'), `${name}: fields come from the same bound source`);
    assert.ok(!text.includes('熱量：111 kcal'), `${name}: legacy nutrition is not merged or first-wins`);
    assert.ok(text.includes('重複來源資料不一致'), `${name}: divergent duplicate remains visibly disclosed`);
    assert.equal(walk(app.cases).filter(el => el.className.includes('meal-card')).length, 1, `${name}: identity renders once`);
    assert.equal(find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片'), undefined, `${name}: divergent duplicate photo stays closed`);
  }
}

async function duplicateConflictMatrixFailsClosedWithoutSyntheticCardsOrPhotos() {
  const base = {log_id: 'conflict', food_log_version: 3, local_date: '2026-09-02', normalized_meal_slot: '早餐', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 321, protein_g: 23}};
  const conflicts = [
    ['date', {...base, local_date: '2026-09-03'}],
    ['slot', {...base, normalized_meal_slot: '晚餐'}],
    ['nutrition', {...base, nutrition_snapshot: {calories_kcal: 999, protein_g: 9}}],
    ['readable trust', {...base, trust_type: 'user_confirmed_ai_estimate', estimate_schema_version: 'meal-photo-user-confirmation-v2', estimate: {calories_kcal: 444, protein_g: 44}}],
  ];
  for (const [name, other] of conflicts) {
    for (const logs of [[base, other], [other, base]]) {
      const body = {...detail, source_logs: logs, source_integrity: {referenced_count: 2, available_snapshot_count: 2, all_snapshots_available: true}};
      const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(body));
      const text = renderedText(app.cases);
      assert.ok(text.includes('來源資料衝突'), `${name}: conflict is visible in either order`);
      assert.ok(text.includes('未顯示營養或照片'), `${name}: fail-closed consequence is explicit`);
      assert.equal(walk(app.ids.timelineTabs).filter(el => el.tagName === 'BUTTON').length, 0, `${name}: no first-wins date projection`);
      assert.ok(!text.includes('熱量：321 kcal'), `${name}: no nutrition from one side`);
      assert.ok(!text.includes('熱量：999 kcal'), `${name}: no contradictory nutrition`);
      assert.ok(!text.includes('約444 kcal'), `${name}: no synthetic trust/nutrition card`);
      assert.equal(find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片'), undefined, `${name}: contradictory photo is not exposed`);
      assert.equal(walk(app.cases).filter(el => el.className.includes('meal-card')).length, 1, `${name}: identity is represented exactly once`);
    }
  }

  const reordered = {nutrition_snapshot: {protein_g: 23, calories_kcal: 321}, trust_type: 'verified_snapshot', normalized_meal_slot: '早餐', local_date: '2026-09-02', food_log_version: 3, log_id: 'conflict'};
  const identicalBody = {...detail, source_logs: [base, reordered], source_integrity: {referenced_count: 2, available_snapshot_count: 2, all_snapshots_available: true}};
  const identicalApp = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(identicalBody));
  assert.equal(walk(identicalApp.cases).filter(el => el.className.includes('meal-card')).length, 1, 'identical verified duplicates deduplicate despite object key order');
  assert.ok(renderedText(identicalApp.cases).includes('熱量：321 kcal'));
  assert.ok(!renderedText(identicalApp.cases).includes('來源資料衝突'));
  assert.ok(find(identicalApp.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片'), 'identical duplicate retains the one source photo control');

  const unboundA = {log_id: 'unbound', food_log_version: 2, trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 100}};
  const unboundB = {...unboundA, nutrition_snapshot: {calories_kcal: 200}};
  const unboundBody = {...detail, source_logs: [unboundA, unboundB], source_integrity: {referenced_count: 2, available_snapshot_count: 2, all_snapshots_available: true}};
  const unboundApp = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(unboundBody));
  const unboundText = renderedText(unboundApp.cases);
  assert.ok(unboundText.includes('來源資料衝突'), 'divergent unbound duplicates are not merged');
  assert.ok(!unboundText.includes('熱量：100 kcal'));
  assert.ok(!unboundText.includes('熱量：200 kcal'));

  const unknownIdentityLogs = [
    {log_id: 'missing-version', local_date: '2026-09-02', normalized_meal_slot: '早餐', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 101}},
    {log_id: 'missing-version', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 202}},
    {food_log_version: 1, trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 303}},
  ];
  const unknownBody = {...detail, source_logs: unknownIdentityLogs, source_integrity: {referenced_count: 3, available_snapshot_count: 3, all_snapshots_available: true}};
  const unknownApp = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(unknownBody));
  const unknownText = renderedText(unknownApp.cases);
  assert.equal(walk(unknownApp.cases).filter(el => el.className.includes('meal-card')).length, 3, 'missing identity fields never collapse unrelated records');
  assert.equal(walk(unknownApp.ids.timelineTabs).filter(el => el.tagName === 'BUTTON').length, 0, 'missing identity fields cannot establish timeline binding');
  for (const calories of [101, 202, 303]) assert.ok(unknownText.includes(`熱量：${calories} kcal`), `unknown identity ${calories} remains visible`);
  assert.ok(unknownText.includes('來源識別不完整'));
}

async function switchingTimelineDateBlocksLatePhoto() {
  const pendingBlob = deferred();
  const dated = {...detail, source_logs: [
    {log_id: 'first', food_log_version: 1, local_date: '2026-09-02', normalized_meal_slot: '早餐', trust_type: 'verified_snapshot', nutrition_snapshot: {}},
    {log_id: 'second', food_log_version: 1, local_date: '2026-09-03', normalized_meal_slot: '午餐', trust_type: 'verified_snapshot', nutrition_snapshot: {}},
  ], source_integrity: {referenced_count: 2, available_snapshot_count: 2, all_snapshots_available: true}};
  const calls = [];
  const app = await createApp((path, options) => {
    calls.push({path, options});
    if (path.includes('/sources/')) return Promise.resolve({ok: true, status: 200, blob: () => pendingBlob.promise});
    return Promise.resolve(path.includes('?') ? jsonResponse(listing) : jsonResponse(dated));
  });
  const firstView = find(app.ids.timelineDays, el => el.tagName === 'BUTTON' && el.textContent === '查看照片');
  const click = firstView.dispatch('click');
  await settle();
  const imageCall = calls.find(call => call.path.includes('/sources/'));
  const tabs = walk(app.ids.timelineTabs).filter(el => el.tagName === 'BUTTON');
  await tabs[1].dispatch('click');
  assert.equal(imageCall.options.signal.aborted, true, 'date switch aborts prior photo ownership');
  pendingBlob.resolve({kind: 'late-jpeg'});
  await click;
  await settle();
  assert.equal(app.objectUrls.length, 0, 'late blob cannot create an object URL');
  assert.equal(find(app.ids.timelineDays, el => el.tagName === 'IMG'), undefined);
}

async function chineseCardsAndPhotoStatusCopy() {
  for (const [status, message] of [
    ['collecting', '收集中，照片尚未開放'],
    ['delivered', '案件已送達，照片不再開放'],
    ['expired', '案件已過期，照片不再開放'],
    ['cancelled', '案件已取消，照片不再開放'],
  ]) {
    const calls = [];
    const caseListing = {items: [{...listing.items[0], status}]};
    const app = await createApp(async (path, options) => {
      calls.push({path, options});
      return path.includes('?') ? jsonResponse(caseListing) : jsonResponse({...detail, status});
    });
    assert.ok(renderedText(app.cases).includes(message), status);
    assert.equal(find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片'), undefined, status);
    assert.equal(calls.filter(call => call.path.includes('/sources/')).length, 0, status);
  }

  const approvedDetail = {
    ...detail,
    source_logs: [{
      log_id: '<img src=x onerror=alert(1)>', food_log_version: 9,
      local_date: '2099-12-31', meal_slot: '晚餐', approval_status: 'approved',
      nutrition_snapshot: {calories_kcal: 510, protein_g: 28, fat_g: 16, carbohydrate_g: 62, fiber_g: 7, sodium_mg: 820},
    }],
  };
  const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(approvedDetail));
  const text = renderedText(app.cases);
  assert.ok(text.includes('營養師核准'));
  assert.ok(!text.includes('非營養師核准'));
  const mealCard = find(app.cases, el => el.className === 'meal-card');
  const mealText = renderedText(mealCard);
  assert.ok(mealText.includes('餐點紀錄'));
  assert.ok(mealText.includes('日期／餐別尚無可驗證資料'));
  assert.ok(!mealText.includes('2099-12-31'));
  assert.ok(!mealText.includes('晚餐'));
  for (const expected of ['熱量：510 kcal', '蛋白質：28 g', '脂肪：16 g', '碳水化合物：62 g', '膳食纖維：7 g', '鈉：820 mg']) assert.ok(text.includes(expected), expected);
  assert.equal(find(app.cases, el => el.tagName === 'IMG'), undefined, 'identifier text cannot create markup');
  const technical = find(app.cases, el => el.tagName === 'DETAILS' && renderedText(el).includes('來源明細（選用）'));
  assert.ok(technical, 'long identifiers and JSON are placed in technical details');
  assert.equal(technical.attributes.open, undefined, 'technical details are collapsed by default');
  assert.ok(renderedText(technical).includes('<img src=x onerror=alert(1)>'), 'escaped text remains inspectable');
}

async function compactPeriodAndMealCardPresentation() {
  const datedListing = {items: [{
    ...listing.items[0],
    window_started_at: '2026-09-02T01:05:00Z',
    window_ends_at: '2026-09-04T16:30:00+08:00',
  }]};
  const twoSourceDetail = {
    ...detail,
    source_logs: [
      detail.source_logs[0],
      {log_id: 'second', trust_type: 'verified_snapshot', nutrition_snapshot: {calories_kcal: 420}},
    ],
    source_integrity: {referenced_count: 2, available_snapshot_count: 2, all_snapshots_available: true},
  };
  const app = await createApp(async path => path.includes('?') ? jsonResponse(datedListing) : jsonResponse(twoSourceDetail));
  const text = renderedText(app.cases);
  assert.ok(text.includes('開始：2026/09/02 09:05'), 'UTC start is shown in Asia/Taipei');
  assert.ok(text.includes('截止：2026/09/04 16:30'), 'offset end preserves the Asia/Taipei instant');
  assert.ok(!text.includes('2026-09-02T01:05:00Z'), 'raw ISO start is not shown');
  assert.ok(!text.includes('2026-09-04T16:30:00+08:00'), 'raw ISO end is not shown');

  const mealCards = walk(app.cases).filter(el => el.className === 'meal-card');
  assert.equal(mealCards.length, 2);
  assert.ok(renderedText(mealCards[0]).includes('餐點紀錄 1'));
  assert.ok(renderedText(mealCards[1]).includes('餐點紀錄 2'));
  for (const card of mealCards) {
    assert.ok(!renderedTextOutsideDetails(card).includes('日期／餐別尚無可驗證資料'), 'technical hint stays out of the primary card');
    const technical = find(card, el => el.tagName === 'DETAILS' && renderedText(el).includes('來源明細（選用）'));
    assert.ok(technical);
    assert.equal(technical.attributes.open, undefined, 'technical details are collapsed by default');
    const summary = find(technical, el => el.tagName === 'SUMMARY' && el.textContent === '來源明細（選用）');
    assert.ok(summary, 'native summary remains available to toggle the details');
    assert.equal(summary.listeners.click, undefined, 'no handler overrides or force-closes the native details state');
    assert.ok(renderedText(technical).includes('日期／餐別尚無可驗證資料'));
  }

  const invalidListing = {items: [{
    ...listing.items[0],
    window_started_at: 'not-a-timestamp',
    window_ends_at: null,
  }]};
  const invalidApp = await createApp(async path => path.includes('?') ? jsonResponse(invalidListing) : jsonResponse(detail));
  const invalidText = renderedText(invalidApp.cases);
  assert.ok(invalidText.includes('開始：未提供'));
  assert.ok(invalidText.includes('截止：未提供'));
  assert.ok(!invalidText.includes('Invalid Date'));
  assert.ok(!invalidText.includes('not-a-timestamp'));
}

async function taipeiTimestampRejectsNormalizedDateTimeFields() {
  const app = await createApp(async path => path.includes('?') ? jsonResponse({items: []}) : jsonResponse(detail));
  const invalidCases = [
    ['24 hour at midnight', '2026-09-02T24:00:00+08:00'],
    ['24 hour with minutes', '2026-09-02T24:01:00+08:00'],
    ['minute 60', '2026-09-02T23:60:00+08:00'],
    ['second 60', '2026-09-02T23:59:60+08:00'],
    ['month 13', '2026-13-02T00:00:00+08:00'],
    ['invalid month day', '2026-04-31T00:00:00+08:00'],
    ['non-leap-year February 29', '2026-02-29T00:00:00+08:00'],
    ['offset hour 24', '2026-09-02T00:00:00+24:00'],
    ['offset minute 60', '2026-09-02T00:00:00-08:60'],
  ];
  for (const [name, value] of invalidCases) {
    assert.equal(app.taipeiTimestamp(value), '未提供', name);
  }

  const validCases = [
    ['UTC crosses into next Taipei day', '2026-09-02T16:00:00Z', '2026/09/03 00:00'],
    ['positive offset crosses into prior Taipei day', '2026-09-03T00:30:00+09:00', '2026/09/02 23:30'],
    ['negative offset crosses into next Taipei day', '2026-09-02T23:30:00-02:00', '2026/09/03 09:30'],
    ['maximum positive offset is accepted', '2026-09-02T00:00:00+23:59', '2026/09/01 08:01'],
    ['maximum negative offset is accepted', '2026-09-02T00:00:00-23:59', '2026/09/03 07:59'],
    ['fractional seconds on leap day', '2024-02-29T00:00:00.123+08:00', '2024/02/29 00:00'],
  ];
  for (const [name, value, expected] of validCases) {
    assert.equal(app.taipeiTimestamp(value), expected, name);
  }
}

async function staleRequestCannotAttach() {
  const pendingImage = deferred();
  const calls = [];
  const app = await createApp((path, options) => {
    calls.push({path, options});
    if (path.includes('/sources/')) return pendingImage.promise;
    if (path.includes('?')) return Promise.resolve(jsonResponse(listing));
    return Promise.resolve(jsonResponse(detail));
  });
  const view = find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片');
  const clickPromise = view.dispatch('click');
  await settle();
  const imageCall = calls.find(call => call.path.includes('/sources/'));
  assert.equal(imageCall.options.signal.aborted, false);
  const refreshPromise = app.refresh.dispatch('click');
  await settle();
  assert.equal(imageCall.options.signal.aborted, true, 'reload aborts outstanding photo fetch');
  pendingImage.resolve(imageResponse());
  await Promise.all([clickPromise, refreshPromise]);
  await settle();
  assert.equal(app.objectUrls.length, 0, 'late response cannot create/attach an object URL');
  assert.equal(find(app.cases, el => el.tagName === 'IMG'), undefined);
}

async function reloadRevokesLoadedPhoto() {
  const app = await createApp(async path => path.includes('/sources/') ? imageResponse() : path.includes('?') ? jsonResponse(listing) : jsonResponse(detail));
  await find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片').dispatch('click');
  assert.equal(app.objectUrls.length, 1);
  await app.refresh.dispatch('click');
  assert.deepEqual(app.revoked, ['blob:test-1']);
  assert.equal(find(app.cases, el => el.tagName === 'IMG'), undefined);
}

async function switchingCasesRevokesPriorCasePhotos() {
  const twoCases = {items: [
    {case_id: 'case-A', status: 'ready_for_review', valid_day_count: 3, window_started_at: 'start', window_ends_at: 'end'},
    {case_id: 'case-B', status: 'ready_for_review', valid_day_count: 3, window_started_at: 'start', window_ends_at: 'end'},
  ]};
  const app = await createApp(async path => {
    if (path.includes('/sources/')) return imageResponse();
    if (path.includes('?')) return jsonResponse(twoCases);
    const caseId = path.endsWith('/case-A') ? 'A' : 'B';
    return jsonResponse({...detail, case_id: `case-${caseId}`, status: 'ready_for_review', source_logs: [{log_id: `log-${caseId}`, trust_type: 'user_confirmed_ai_estimate', estimate: {}}]});
  });
  const views = walk(app.cases).filter(el => el.tagName === 'BUTTON' && el.textContent === '查看照片');
  assert.equal(views.length, 2);

  await views[0].dispatch('click');
  assert.equal(walk(app.cases).filter(el => el.tagName === 'IMG').length, 1);
  await views[1].dispatch('click');

  const images = walk(app.cases).filter(el => el.tagName === 'IMG');
  assert.deepEqual(app.revoked, ['blob:test-1'], 'switching case revokes the prior case URL');
  assert.equal(images.length, 1, 'only the active case photo remains mounted');
  assert.equal(images[0].attributes.src, 'blob:test-2');
}

async function latePriorCaseResponseCannotAttach() {
  const pendingA = deferred();
  const calls = [];
  const twoCases = {items: [
    {case_id: 'case-A', status: 'ready_for_review', valid_day_count: 3, window_started_at: 'start', window_ends_at: 'end'},
    {case_id: 'case-B', status: 'ready_for_review', valid_day_count: 3, window_started_at: 'start', window_ends_at: 'end'},
  ]};
  const app = await createApp((path, options) => {
    calls.push({path, options});
    if (path.includes('/case-A/sources/')) return pendingA.promise;
    if (path.includes('/case-B/sources/')) return Promise.resolve(imageResponse());
    if (path.includes('?')) return Promise.resolve(jsonResponse(twoCases));
    const caseId = path.endsWith('/case-A') ? 'A' : 'B';
    return Promise.resolve(jsonResponse({...detail, case_id: `case-${caseId}`, status: 'ready_for_review', source_logs: [{log_id: `log-${caseId}`, trust_type: 'user_confirmed_ai_estimate', estimate: {}}]}));
  });
  const views = walk(app.cases).filter(el => el.tagName === 'BUTTON' && el.textContent === '查看照片');
  const clickA = views[0].dispatch('click');
  await settle();
  const callA = calls.find(call => call.path.includes('/case-A/sources/'));
  assert.equal(callA.options.signal.aborted, false);

  await views[1].dispatch('click');
  assert.equal(callA.options.signal.aborted, true, 'switching case aborts the prior case request');
  pendingA.resolve(imageResponse());
  await clickA;
  await settle();

  const images = walk(app.cases).filter(el => el.tagName === 'IMG');
  assert.equal(app.objectUrls.length, 1, 'late prior-case response creates no leaked object URL');
  assert.equal(images.length, 1);
  assert.equal(images[0].attributes.src, 'blob:test-1');
}

async function cancelledCaseRequestCanRetryWithoutOldFinallyUnlockingIt() {
  const pendingOldA = deferred();
  const pendingNewA = deferred();
  const calls = [];
  const twoCases = {items: [
    {case_id: 'case-A', status: 'ready_for_review', valid_day_count: 3, window_started_at: 'start', window_ends_at: 'end'},
    {case_id: 'case-B', status: 'ready_for_review', valid_day_count: 3, window_started_at: 'start', window_ends_at: 'end'},
  ]};
  let aRequestCount = 0;
  const app = await createApp((path, options) => {
    calls.push({path, options});
    if (path.includes('/case-A/sources/')) {
      aRequestCount += 1;
      return aRequestCount === 1 ? pendingOldA.promise : pendingNewA.promise;
    }
    if (path.includes('/case-B/sources/')) return Promise.resolve(imageResponse());
    if (path.includes('?')) return Promise.resolve(jsonResponse(twoCases));
    const caseId = path.endsWith('/case-A') ? 'A' : 'B';
    return Promise.resolve(jsonResponse({...detail, case_id: `case-${caseId}`, status: 'ready_for_review', source_logs: [{log_id: `log-${caseId}`, trust_type: 'user_confirmed_ai_estimate', estimate: {}}]}));
  });
  const views = walk(app.cases).filter(el => el.tagName === 'BUTTON' && el.textContent === '查看照片');

  const oldClickA = views[0].dispatch('click');
  await settle();
  assert.equal(views[0].disabled, true);
  await views[1].dispatch('click');
  assert.equal(views[0].disabled, false, 'case switch immediately restores the cancelled button');

  const newClickA = views[0].dispatch('click');
  await settle();
  assert.equal(aRequestCount, 2, 'cancelled case can be retried before its old promise settles');
  assert.equal(views[0].disabled, true);

  pendingOldA.resolve(imageResponse());
  await oldClickA;
  await settle();
  assert.equal(views[0].disabled, true, 'old finally cannot unlock the replacement request');
  assert.equal(walk(app.cases).filter(el => el.tagName === 'IMG').length, 0, 'old response cannot attach');

  pendingNewA.resolve(imageResponse());
  await newClickA;
  await settle();
  assert.equal(views[0].disabled, false);
  const images = walk(app.cases).filter(el => el.tagName === 'IMG');
  assert.equal(images.length, 1);
  assert.equal(images[0].attributes.src, 'blob:test-2');
}

async function pagehideRevokes() {
  const app = await createApp(async path => path.includes('/sources/') ? imageResponse() : path.includes('?') ? jsonResponse(listing) : jsonResponse(detail));
  await find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片').dispatch('click');
  assert.equal(app.objectUrls.length, 1);
  for (const handler of app.windowListeners.pagehide || []) handler();
  assert.deepEqual(app.revoked, ['blob:test-1']);
  assert.equal(find(app.cases, el => el.tagName === 'IMG'), undefined);
}

async function errorControls() {
  for (const [status, expected, retry] of [
    [401, 'LINE身分驗證失敗', false],
    [403, '此LINE帳號未獲營養師唯讀權限', false],
    [404, '照片不可用', false],
    [503, '照片暫時無法載入，請重試', true],
  ]) {
    let imageStatus = status;
    const app = await createApp(async path => {
      if (path.includes('/sources/')) return imageResponse(imageStatus);
      return path.includes('?') ? jsonResponse(listing) : jsonResponse(detail);
    });
    const view = find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片');
    await view.dispatch('click');
    assert.equal(app.objectUrls.length, 0, `${status} must not look successful`);
    assert.ok(find(app.cases, el => el.textContent === expected), `${status} shows controlled message`);
    if (retry) {
      assert.equal(view.disabled, false, '503 remains retryable');
      imageStatus = 200;
      await view.dispatch('click');
      assert.equal(app.objectUrls.length, 1, 'retry can succeed');
    }
  }

  let imageStatus = 200;
  const app = await createApp(async path => {
    if (path.includes('/sources/')) return imageResponse(imageStatus);
    return path.includes('?') ? jsonResponse(listing) : jsonResponse(detail);
  });
  const view = find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '查看照片');
  await view.dispatch('click');
  imageStatus = 401;
  await view.dispatch('click');
  assert.deepEqual(app.revoked, ['blob:test-1'], 'authorization failure revokes the prior preview');
  assert.equal(find(app.cases, el => el.tagName === 'IMG'), undefined);
  assert.ok(find(app.cases, el => el.textContent === 'LINE身分驗證失敗'));
}

function sourceLogs(count) {
  return Array.from({length: count}, (_, index) => ({
    log_id: `source-${index + 1}`,
    trust_type: 'verified_snapshot',
    nutrition_snapshot: {},
  }));
}

async function sourceIntegrityCopy() {
  const cases = [
    {
      name: 'complete',
      itemCount: 3,
      detail: {valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: sourceLogs(6), source_integrity: {referenced_count: 6, available_snapshot_count: 6, all_snapshots_available: true}},
      present: ['有效日：3 / 3', '保留來源參照：6 筆', '目前可驗證快照：6 筆'],
    },
    {
      name: 'partial',
      itemCount: 3,
      detail: {valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: sourceLogs(5), source_integrity: {referenced_count: 6, available_snapshot_count: 5, all_snapshots_available: false}},
      present: ['有效日：3 / 3', '保留來源參照：6 筆', '目前可驗證快照：5 筆'],
    },
    {
      name: 'none currently available',
      itemCount: 3,
      detail: {valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: [], source_integrity: {referenced_count: 6, available_snapshot_count: 0, all_snapshots_available: false}},
      present: ['有效日：3 / 3', '保留來源參照：6 筆', '目前可驗證快照：0 筆', '目前無可顯示的來源快照；不代表沒有飲食紀錄'],
      absent: ['目前沒有可驗證來源快照'],
    },
  ];
  for (const scenario of cases) {
    const caseListing = {items: [{...listing.items[0], valid_day_count: scenario.itemCount}]};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(caseListing) : jsonResponse({case_id: 'case/A', status: 'ready_for_review', ...scenario.detail}));
    const text = renderedText(app.cases);
    for (const expected of scenario.present) assert.ok(text.includes(expected), `${scenario.name}: ${expected}`);
    for (const forbidden of scenario.absent || []) assert.ok(!text.includes(forbidden), `${scenario.name}: ${forbidden}`);
  }
}

async function invalidSourceLogsNeverMasqueradeAsZeroRecords() {
  const invalidSourceLogs = [
    ['missing source logs', undefined],
    ['null source logs', null],
    ['object source logs', {}],
    ['string source logs', ''],
  ];
  for (const [name, sourceLogsValue] of invalidSourceLogs) {
    const body = {valid_day_count: 3, valid_days: threeQualifiedDays, source_integrity: {referenced_count: 0, available_snapshot_count: 0, all_snapshots_available: true}};
    if (sourceLogsValue !== undefined) body.source_logs = sourceLogsValue;
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse({case_id: 'case/A', status: 'ready_for_review', ...body}));
    const text = renderedText(app.cases);
    assert.ok(text.includes('保留來源參照：資料不可用'), name);
    assert.ok(text.includes('目前可驗證快照：資料不可用'), name);
    assert.ok(text.includes('來源快照清單資料不可用'), name);
    assert.ok(!text.includes('保留來源參照：0 筆'), `${name}: malformed records must not prove zero references`);
    assert.ok(!text.includes('目前可驗證快照：0 筆'), `${name}: malformed records must not prove zero snapshots`);
  }

  {
    const body = {valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: [], source_integrity: {referenced_count: 0, available_snapshot_count: 0, all_snapshots_available: true}};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse({case_id: 'case/A', status: 'ready_for_review', ...body}));
    const text = renderedText(app.cases);
    assert.ok(text.includes('保留來源參照：0 筆'));
    assert.ok(text.includes('目前可驗證快照：0 筆'));
    assert.ok(!text.includes('來源快照清單資料不可用'));
  }

  {
    const body = {valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: null, source_integrity: {referenced_count: 6, available_snapshot_count: 0, all_snapshots_available: false}};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse({case_id: 'case/A', status: 'ready_for_review', ...body}));
    const text = renderedText(app.cases);
    assert.ok(text.includes('保留來源參照：資料不可用'));
    assert.ok(text.includes('目前可驗證快照：資料不可用'));
    assert.ok(text.includes('來源快照清單資料不可用'));
    assert.ok(!text.includes('保留來源參照：6 筆'));
  }
}

async function invalidSourceCountsNeverFallBackToRenderedLogLength() {
  const invalidDetails = [
    ['missing referenced count', {available_snapshot_count: 1, all_snapshots_available: false}],
    ['missing available count', {referenced_count: 6, all_snapshots_available: false}],
    ['wrong referenced count type', {referenced_count: '6', available_snapshot_count: 1, all_snapshots_available: false}],
    ['wrong available count type', {referenced_count: 6, available_snapshot_count: '1', all_snapshots_available: false}],
    ['negative count', {referenced_count: 6, available_snapshot_count: -1, all_snapshots_available: false}],
    ['noninteger count', {referenced_count: 6, available_snapshot_count: 0.5, all_snapshots_available: false}],
    ['available exceeds referenced', {referenced_count: 0, available_snapshot_count: 1, all_snapshots_available: false}],
    ['count differs from snapshots', {referenced_count: 6, available_snapshot_count: 2, all_snapshots_available: false}],
    ['completeness boolean inconsistent', {referenced_count: 1, available_snapshot_count: 1, all_snapshots_available: false}],
  ];
  for (const [name, integrity] of invalidDetails) {
    const body = {valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: sourceLogs(1), source_integrity: integrity};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse({case_id: 'case/A', status: 'ready_for_review', ...body}));
    const text = renderedText(app.cases);
    assert.ok(text.includes('保留來源參照：資料不可用'), name);
    assert.ok(text.includes('目前可驗證快照：資料不可用'), name);
    assert.ok(!text.includes('目前可驗證快照：1 筆'), `${name}: must not use source_logs.length`);
  }

  for (const [name, itemCount, detailCount] of [
    ['missing detail count', 3, undefined],
    ['wrong detail count type', 3, '3'],
    ['negative detail count', 3, -1],
    ['noninteger detail count', 3, 2.5],
    ['list/detail mismatch', 2, 3],
  ]) {
    const caseListing = {items: [{...listing.items[0], valid_day_count: itemCount}]};
    const body = {valid_day_count: detailCount, valid_days: threeQualifiedDays, source_logs: [], source_integrity: {referenced_count: 0, available_snapshot_count: 0, all_snapshots_available: true}};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(caseListing) : jsonResponse({case_id: 'case/A', status: 'ready_for_review', ...body}));
    const text = renderedText(app.cases);
    assert.ok(text.includes('有效日：資料不可用'), name);
    assert.ok(!text.includes('有效日：3 / 3'), `${name}: must not trust list fallback`);
  }
}

async function validDayCountRequiresConsistentDayRecords() {
  const validCases = [
    ['three qualified days', 3, threeQualifiedDays, ['有效日：3 / 3', '2026-09-02｜2 餐｜符合', '2026-09-04｜2 餐｜符合']],
    ['zero days', 0, [], ['有效日：0 / 3', '目前尚無可列入的日期']],
    ['incomplete day does not count', 0, [validDay('2026-09-02', 'incomplete', 1)], ['有效日：0 / 3', '2026-09-02｜1 餐｜未符合']],
  ];
  for (const [name, count, validDays, expectedTexts] of validCases) {
    const caseListing = {items: [{...listing.items[0], valid_day_count: count}]};
    const body = {valid_day_count: count, valid_days: validDays, source_logs: []};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(caseListing) : jsonResponse({case_id: 'case/A', status: 'ready_for_review', ...body}));
    const text = renderedText(app.cases);
    for (const expected of expectedTexts) assert.ok(text.includes(expected), `${name}: ${expected}`);
  }

  const invalidCases = [
    ['count three with empty days', 3, []],
    ['missing days', 3, undefined],
    ['wrong days type', 3, {}],
    ['count differs from qualified days', 2, threeQualifiedDays],
    ['malformed day record', 1, [{local_date: '2026-09-02', completeness_status: 'qualified'}]],
  ];
  for (const [name, count, validDays] of invalidCases) {
    const caseListing = {items: [{...listing.items[0], valid_day_count: count}]};
    const body = {valid_day_count: count, source_logs: []};
    if (validDays !== undefined) body.valid_days = validDays;
    const app = await createApp(async path => path.includes('?') ? jsonResponse(caseListing) : jsonResponse({case_id: 'case/A', status: 'ready_for_review', ...body}));
    const text = renderedText(app.cases);
    assert.ok(text.includes('有效日：資料不可用'), name);
    assert.ok(!text.includes(`有效日：${count} / 3`), `${name}: contradictory count must not render`);
  }
}

async function supplementOnlyDirtyNavigationMatrix() {
  const savedDraft = {
    ...detail, source_token: '9'.repeat(64), current_review_version: 9,
    latest_review_available: true, latest_review_fresh: true,
    latest_review: {review_version: 9, status: 'draft', review: {good: '好', priority: '先做', next_7_days: '七天', comment: '點評'}, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'},
    supplement_request: null,
  };
  const twoCases = {items: [listing.items[0], {...listing.items[0], case_id: 'case-B'}]};
  const draftFields = {good: '好', priority: '先做', next_7_days: '七天', comment: '點評'};

  for (const [dirtyId, dirtyValue] of [['supplementReason', '只改補件理由'], ['supplementRequiredContent', '只改補件內容']]) {
    for (const action of ['back', 'refresh', 'case-switch']) {
      const calls = []; const confirmations = [];
      const app = await createApp(async (path, options = {}) => {
        calls.push({path, options});
        if (path.includes('?')) return jsonResponse(twoCases);
        return jsonResponse({...savedDraft, case_id: path.endsWith('/case-B') ? 'case-B' : 'case/A'});
      }, {autoOpen: false, confirmImpl: message => { confirmations.push(message); return false; }});
      let rows = walk(app.cases).filter(el => el.className === 'case-row');
      await rows[0].dispatch('click');
      for (const [id, value] of Object.entries(draftFields)) assert.equal(app.ids[id].value, value, `${dirtyId}/${action}: generic draft starts clean`);
      assert.ok(!app.ids.draftState.textContent.includes('未儲存'), `${dirtyId}/${action}: guard must not depend on generic draft dirty`);
      app.ids[dirtyId].value = dirtyValue; await app.ids[dirtyId].dispatch('input');
      if (action === 'back') await app.ids.backQueue.dispatch('click');
      if (action === 'refresh') await app.refresh.dispatch('click');
      if (action === 'case-switch') { rows = walk(app.cases).filter(el => el.className === 'case-row'); await rows[1].dispatch('click'); }
      assert.equal(confirmations.length, 1, `${dirtyId}/${action}: pure supplement dirty asks once before discard`);
      assert.ok(confirmations[0].includes('未提交的補件輸入'), `${dirtyId}/${action}: confirmation identifies supplement input`);
      assert.equal(app.ids[dirtyId].value, dirtyValue, `${dirtyId}/${action}: cancelled navigation retains supplement input`);
      assert.equal(app.ids.review.hidden, false, `${dirtyId}/${action}: cancelled navigation keeps the current case open`);
      assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, `${dirtyId}/${action}: cancelled navigation never secretly saves or submits`);
    }

    const calls = [];
    const app = await createApp(async (path, options = {}) => {
      calls.push({path, options});
      return path.includes('?') ? jsonResponse(listing) : jsonResponse(savedDraft);
    }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    for (const [id, value] of Object.entries(draftFields)) assert.equal(app.ids[id].value, value, `${dirtyId}/beforeunload: generic draft starts clean`);
    app.ids[dirtyId].value = dirtyValue; await app.ids[dirtyId].dispatch('input');
    const event = {prevented: false, returnValue: undefined, preventDefault() { this.prevented = true; }};
    for (const handler of app.windowListeners.beforeunload || []) handler(event);
    assert.equal(event.prevented, true, `${dirtyId}/beforeunload: pure supplement dirty blocks unload`);
    assert.equal(event.returnValue, '', `${dirtyId}/beforeunload: browser receives unload cancellation signal`);
    assert.equal(app.ids[dirtyId].value, dirtyValue, `${dirtyId}/beforeunload: supplement input is not cleared`);
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, `${dirtyId}/beforeunload: unload guard never secretly saves or submits`);
  }
}

async function supplementLateResponseOwnershipMatrix() {
  const savedDraft = {
    ...detail, source_token: '8'.repeat(64), current_review_version: 8,
    latest_review_available: true, latest_review_fresh: true,
    latest_review: {review_version: 8, status: 'draft', review: {good: '好', priority: '先做', next_7_days: '七天', comment: '點評'}, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'},
    supplement_request: null,
  };
  const fields = {reason: '晚回應理由', required_content: '晚回應內容'};
  const requested = {...savedDraft, status: 'needs_more_info', supplement_request: {...fields, notification_status: 'not_sent', notified: false, requested_at: '2026-09-04T02:00:00+08:00'}};

  {
    const pendingPost = deferred(); const calls = []; let listCalls = 0; let detailCalls = 0;
    const app = await createApp((path, options = {}) => {
      calls.push({path, options});
      if (path.includes('?')) { listCalls += 1; return Promise.resolve(jsonResponse(listCalls === 1 ? listing : {items: []})); }
      if (options.method === 'POST') return pendingPost.promise;
      detailCalls += 1; return Promise.resolve(jsonResponse(savedDraft));
    }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    app.ids.supplementReason.value = fields.reason; await app.ids.supplementReason.dispatch('input');
    app.ids.supplementRequiredContent.value = fields.required_content; await app.ids.supplementRequiredContent.dispatch('input');
    const submit = app.ids.requestMoreInfo.dispatch('click'); await settle();
    await app.refresh.dispatch('click');
    pendingPost.resolve(jsonResponse({status: 'needs_more_info', notification_status: 'not_sent', created: true}));
    await submit;
    assert.equal(detailCalls, 1, 'late supplement POST after app-generation refresh must not start canonical GET');
    assert.equal(app.ids.queue.hidden, false, 'late supplement POST cannot reopen old detail after refresh');
    assert.equal(app.ids.review.hidden, true, 'late supplement POST remains fenced by app generation');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 1);
  }

  {
    const pendingReadback = deferred(); let posted = false; let listCalls = 0; let detailCalls = 0;
    const app = await createApp((path, options = {}) => {
      if (path.includes('?')) { listCalls += 1; return Promise.resolve(jsonResponse(listCalls === 1 ? listing : {items: []})); }
      if (options.method === 'POST') { posted = true; return Promise.resolve(jsonResponse({status: 'needs_more_info', notification_status: 'not_sent', created: true})); }
      detailCalls += 1;
      return posted ? pendingReadback.promise : Promise.resolve(jsonResponse(savedDraft));
    }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    app.ids.supplementReason.value = fields.reason; await app.ids.supplementReason.dispatch('input');
    app.ids.supplementRequiredContent.value = fields.required_content; await app.ids.supplementRequiredContent.dispatch('input');
    const submit = app.ids.requestMoreInfo.dispatch('click'); await settle();
    assert.equal(detailCalls, 2, 'POST success reaches a pending canonical GET');
    await app.refresh.dispatch('click');
    pendingReadback.resolve(jsonResponse(requested)); await submit;
    assert.equal(app.ids.queue.hidden, false, 'late supplement canonical GET cannot reopen detail after refresh');
    assert.equal(app.ids.review.hidden, true, 'late supplement canonical GET remains fenced by app generation');
    assert.equal(app.ids.supplementReadback.hidden, true, 'late supplement canonical GET cannot paint readback into refreshed app');
  }

  {
    const pendingReadback = deferred(); let posted = false;
    const twoCases = {items: [listing.items[0], {...listing.items[0], case_id: 'case-B'}]};
    const app = await createApp((path, options = {}) => {
      if (path.includes('?')) return Promise.resolve(jsonResponse(twoCases));
      if (options.method === 'POST') { posted = true; return Promise.resolve(jsonResponse({status: 'needs_more_info', notification_status: 'not_sent', created: true})); }
      if (posted && path.endsWith('/case%2FA')) return pendingReadback.promise;
      return Promise.resolve(jsonResponse({...savedDraft, case_id: path.endsWith('/case-B') ? 'case-B' : 'case/A'}));
    }, {autoOpen: false});
    let rows = walk(app.cases).filter(el => el.className === 'case-row'); await rows[0].dispatch('click');
    app.ids.supplementReason.value = fields.reason; await app.ids.supplementReason.dispatch('input');
    app.ids.supplementRequiredContent.value = fields.required_content; await app.ids.supplementRequiredContent.dispatch('input');
    const submit = app.ids.requestMoreInfo.dispatch('click'); await settle();
    rows = walk(app.cases).filter(el => el.className === 'case-row'); await rows[1].dispatch('click');
    pendingReadback.resolve(jsonResponse(requested)); await submit;
    assert.equal(app.ids.review.hidden, false, 'case switch remains on the new detail');
    assert.equal(app.ids.supplementReason.value, '', 'late old-case canonical GET cannot replace new-case supplement baseline');
    assert.equal(app.ids.supplementRequiredContent.value, '', 'late old-case canonical GET cannot paint content into new case');
    assert.equal(app.ids.supplementReadback.hidden, true, 'late old-case canonical GET cannot render old request in new case');
  }
}

async function supplementRequestHttpContractAndLifecycleMatrix() {
  const actualFixture = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  assert.equal(actualFixture.status, 'needs_more_info');
  assert.deepEqual(actualFixture.supplement_request && {
    reason: actualFixture.supplement_request.reason,
    required_content: actualFixture.supplement_request.required_content,
    notification_status: actualFixture.supplement_request.notification_status,
    notified: actualFixture.supplement_request.notified,
  }, {reason: '資料不足以判讀', required_content: '請補早餐照片與份量', notification_status: 'not_sent', notified: false});
  const savedDraft = {
    ...detail, source_token: 'd'.repeat(64), current_review_version: 3,
    latest_review_available: true, latest_review_fresh: true,
    latest_review: {review_version: 3, status: 'draft', review: {good: '好', priority: '先做', next_7_days: '七天', comment: '點評'}, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'},
    supplement_request: null,
  };
  const requested = {...actualFixture, case_id: 'case/A', source_token: savedDraft.source_token, current_review_version: 3, latest_review: savedDraft.latest_review, latest_review_available: true, latest_review_fresh: true};
  const values = {supplementReason: '資料不足以判讀', supplementRequiredContent: '<img src=x onerror=alert(1)>請補早餐照片與份量'};

  {
    const calls = []; const confirmations = []; let posted = false;
    const app = await createApp(async (path, options = {}) => {
      calls.push({path, options});
      if (path.includes('?')) return jsonResponse(listing);
      if (options.method === 'POST') { posted = true; return jsonResponse({status: 'needs_more_info', notification_status: 'not_sent', created: true}); }
      return jsonResponse(posted ? {...requested, supplement_request: {reason: values.supplementReason, required_content: values.supplementRequiredContent, notification_status: 'not_sent', notified: false, requested_at: '2026-09-04T02:00:00+08:00'}} : savedDraft);
    }, {autoOpen: false, confirmImpl: message => { confirmations.push(message); return true; }});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    for (const [key, value] of Object.entries(values)) { app.ids[key].value = value; await app.ids[key].dispatch('input'); }
    await app.ids.requestMoreInfo.dispatch('click');
    const posts = calls.filter(call => call.options.method === 'POST');
    assert.equal(posts.length, 1, 'double-click guard starts with exactly one POST');
    assert.equal(posts[0].path, '/api/dietitian/health-checks/case%2FA/request-more-info');
    assert.deepEqual(JSON.parse(posts[0].options.body), {reason: values.supplementReason, required_content: values.supplementRequiredContent, expected_source_token: savedDraft.source_token, expected_review_version: 3, request_id: 'request-uuid-1'});
    assert.ok(!Object.hasOwn(JSON.parse(posts[0].options.body), 'actor_id'), 'verified actor is server-only');
    assert.ok(confirmations[0].includes('尚未 LINE 通知'));
    assert.equal(calls.filter(call => call.path.endsWith('/case%2FA') && call.options.method !== 'POST').length, 2, 'POST success is provisional until canonical GET');
    assert.ok(app.ids.supplementState.textContent.includes('待補件') && app.ids.supplementState.textContent.includes('尚未 LINE 通知'));
    assert.ok(renderedText(app.ids.supplementReadback).includes(values.supplementRequiredContent), 'untrusted request text is rendered literally through text nodes');
    assert.equal(find(app.ids.supplementReadback, el => el.tagName === 'IMG'), undefined);
  }

  {
    const calls = [];
    const app = await createApp(async (path, options = {}) => { calls.push({path, options}); return path.includes('?') ? jsonResponse(listing) : jsonResponse(savedDraft); }, {autoOpen: false, confirmImpl: () => false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    await app.ids.requestMoreInfo.dispatch('click');
    assert.ok(app.ids.supplementError.textContent.includes('必填'));
    app.ids.supplementReason.value = '理由'; await app.ids.supplementReason.dispatch('input');
    app.ids.supplementRequiredContent.value = '內容'; await app.ids.supplementRequiredContent.dispatch('input');
    app.ids.comment.value = '未存草稿'; await app.ids.comment.dispatch('input');
    await app.ids.requestMoreInfo.dispatch('click');
    assert.ok(app.ids.supplementError.textContent.includes('未儲存草稿'));
    assert.equal(app.ids.comment.value, '未存草稿');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, 'dirty draft is neither saved nor discarded and cancellation sends zero POST');
  }

  {
    const calls = [];
    const app = await createApp(async (path, options = {}) => { calls.push({path, options}); return path.includes('?') ? jsonResponse(listing) : jsonResponse(savedDraft); }, {autoOpen: false, confirmImpl: () => false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    app.ids.supplementReason.value = '理由'; await app.ids.supplementReason.dispatch('input');
    app.ids.supplementRequiredContent.value = '內容'; await app.ids.supplementRequiredContent.dispatch('input');
    await app.ids.requestMoreInfo.dispatch('click');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, 'cancelled confirmation sends zero POST');
    assert.equal(app.ids.supplementReason.value, '理由');
  }

  {
    const calls = []; let posted = false;
    const app = await createApp(async (path, options = {}) => {
      calls.push({path, options});
      if (path.includes('?')) return jsonResponse(listing);
      if (options.method === 'POST') { posted = true; return jsonResponse({status: 'needs_more_info', notification_status: 'not_sent', created: true}); }
      return jsonResponse(posted ? {...requested, supplement_request: {...requested.supplement_request, reason: '另一個理由'}} : savedDraft);
    }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    app.ids.supplementReason.value = '理由'; await app.ids.supplementReason.dispatch('input');
    app.ids.supplementRequiredContent.value = '內容'; await app.ids.supplementRequiredContent.dispatch('input');
    await app.ids.requestMoreInfo.dispatch('click');
    assert.ok(app.ids.supplementError.textContent.includes('結果未知'), 'contradictory GET remains an unknown outcome');
    assert.equal(app.ids.supplementReason.value, '理由', 'wrong GET cannot replace operator input');
    assert.equal(app.ids.supplementReadback.hidden, true, 'wrong GET cannot render a different supplement as confirmed');
  }

  {
    const clock = fakeClock(); const calls = []; let attempt = 0;
    const app = await createApp((path, options = {}) => {
      calls.push({path, options});
      if (path.includes('?')) return Promise.resolve(jsonResponse(listing));
      if (options.method === 'POST') { attempt += 1; return attempt === 1 ? new Promise(() => {}) : Promise.resolve(jsonResponse({}, 409)); }
      return Promise.resolve(jsonResponse(savedDraft));
    }, {autoOpen: false, clock});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    app.ids.supplementReason.value = '理由'; await app.ids.supplementReason.dispatch('input');
    app.ids.supplementRequiredContent.value = '內容'; await app.ids.supplementRequiredContent.dispatch('input');
    const first = app.ids.requestMoreInfo.dispatch('click'); await settle();
    await app.ids.requestMoreInfo.dispatch('click');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 1, 'double click while pending is blocked');
    clock.advance(15000); await first;
    const body = calls.find(call => call.options.method === 'POST').options.body;
    await app.ids.requestMoreInfo.dispatch('click');
    const bodies = calls.filter(call => call.options.method === 'POST').map(call => call.options.body);
    assert.equal(bodies[1], body, 'timeout retry reuses the exact payload and request id');
    assert.ok(app.ids.supplementError.textContent.includes('人工比較'));
    assert.equal(app.ids.supplementReason.value, '理由', '409 preserves input');
    assert.equal(JSON.parse(bodies[1]).expected_source_token, savedDraft.source_token, '409 never swaps server token');
  }

  {
    const pending = deferred(); const calls = [];
    const app = await createApp((path, options = {}) => { calls.push({path, options}); if (path.includes('?')) return Promise.resolve(jsonResponse(listing)); if (options.method === 'POST') return pending.promise; return Promise.resolve(jsonResponse(savedDraft)); }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    app.ids.supplementReason.value = '理由'; await app.ids.supplementReason.dispatch('input');
    app.ids.supplementRequiredContent.value = '內容'; await app.ids.supplementRequiredContent.dispatch('input');
    const submit = app.ids.requestMoreInfo.dispatch('click'); await settle();
    await app.ids.backQueue.dispatch('click');
    pending.resolve(jsonResponse({status: 'needs_more_info', notification_status: 'not_sent', created: true}));
    await submit;
    assert.equal(app.ids.review.hidden, true, 'late completion after case exit cannot repaint');
  }

  {
    const terminalListing = {items: [{...listing.items[0], status: 'delivered'}]}; const calls = [];
    const app = await createApp(async (path, options = {}) => { calls.push({path, options}); return path.includes('?') ? jsonResponse(terminalListing) : jsonResponse({...savedDraft, status: 'delivered', supplement_request: null}); }, {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    assert.equal(app.ids.requestMoreInfo.disabled, true);
    await app.ids.requestMoreInfo.dispatch('click');
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, 'terminal current state cannot request supplement');
    assert.ok(!renderedText(app.ids.supplementReadback).includes('資料不足以判讀'), 'terminal replay never shows historical supplement');
  }
}

async function supplementNotificationPairingAndResolvedReadback() {
  const notSent = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  const delivered = JSON.parse(fs.readFileSync(process.argv[5], 'utf8'));
  const queued = JSON.parse(fs.readFileSync(process.argv[6], 'utf8'));
  assert.deepEqual({status: notSent.supplement_request.notification_status, notified: notSent.supplement_request.notified}, {status: 'not_sent', notified: false});
  assert.deepEqual({status: queued.supplement_request.notification_status, notified: queued.supplement_request.notified}, {status: 'queued', notified: false});
  assert.deepEqual({status: delivered.supplement_request.notification_status, notified: delivered.supplement_request.notified}, {status: 'delivered', notified: true});
  for (const [fixture, expected, forbidden] of [[notSent, '尚未 LINE 通知', '已通知顧客'], [queued, '通知處理中', '已通知顧客'], [delivered, '已通知顧客', '尚未 LINE 通知']]) {
    const listingForFixture = {items: [{...listing.items[0], case_id: fixture.case_id, status: fixture.status}]};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listingForFixture) : jsonResponse(fixture), {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    assert.ok(app.ids.supplementState.textContent.includes(expected), `${fixture.supplement_request.notification_status}: paired notification state`);
    assert.ok(!app.ids.supplementState.textContent.includes(forbidden));
    assert.ok(renderedText(app.ids.supplementReadback).includes(fixture.supplement_request.reason));
  }
  for (const contradictory of [{...notSent, supplement_request: {...notSent.supplement_request, notified: true}}, {...delivered, supplement_request: {...delivered.supplement_request, notified: false}}]) {
    const listingForFixture = {items: [{...listing.items[0], case_id: contradictory.case_id, status: contradictory.status}]};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listingForFixture) : jsonResponse(contradictory), {autoOpen: false});
    await find(app.cases, el => el.className === 'case-row').dispatch('click');
    assert.ok(app.ids.supplementError.textContent.includes('不可驗證'), `${contradictory.supplement_request.notification_status}: contradictory notified flag fails closed`);
    assert.equal(app.ids.supplementReadback.hidden, true);
  }
  {
    const savedDraft = {...detail, source_token: '9'.repeat(64), current_review_version: 3, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 3, status: 'draft', review: {good: '好', priority: '先做', next_7_days: '七天', comment: '點評'}, ai_observations: {}, suggested_values: {}, limitations: '資料限制快照', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'}, supplement_request: null};
    const calls=[];let posted=false;
    const app=await createApp(async(path,options={})=>{calls.push({path,options});if(path.includes('?'))return jsonResponse(listing);if(options.method==='POST'){posted=true;return jsonResponse({status:'needs_more_info',notification_status:'not_sent',created:true})}return jsonResponse(posted?{...delivered,case_id:'case/A',source_token:savedDraft.source_token,current_review_version:3,latest_review:savedDraft.latest_review,latest_review_available:true,latest_review_fresh:true}:savedDraft)}, {autoOpen:false});
    await find(app.cases,el=>el.className==='case-row').dispatch('click');app.ids.supplementReason.value=delivered.supplement_request.reason;await app.ids.supplementReason.dispatch('input');app.ids.supplementRequiredContent.value=delivered.supplement_request.required_content;await app.ids.supplementRequiredContent.dispatch('input');await app.ids.requestMoreInfo.dispatch('click');
    assert.equal(app.ids.supplementError.textContent,'','notification delivered between POST and GET must not be reported as save failure');
    assert.ok(app.ids.supplementState.textContent.includes('已通知顧客'));
  }
  {
    const resolved={...delivered,status:'ready_for_review',latest_review_fresh:false,supplement_request:null};const list={items:[{...listing.items[0],case_id:resolved.case_id,status:'ready_for_review'}]};
    const app=await createApp(async path=>path.includes('?')?jsonResponse(list):jsonResponse(resolved),{autoOpen:false});await find(app.cases,el=>el.className==='case-row').dispatch('click');
    assert.ok(app.ids.supplementState.textContent.includes('待重新審查'));
    assert.equal(isActuallyVisible(app.ids.supplementState),true,'resolved lifecycle state has no hidden ancestor');
    assert.equal(app.ids.supplementControls.hidden,true,'resolved request hides only obsolete supplement controls');
    assert.equal(app.ids.supplementReason.disabled,true);assert.equal(app.ids.supplementRequiredContent.disabled,true);assert.equal(app.ids.requestMoreInfo.disabled,true);
    assert.equal(app.ids.supplementReadback.hidden,true);assert.ok(!renderedText(app.ids.supplementReadback).includes(delivered.supplement_request.reason));
    assert.equal(app.ids.draftEditor.hidden,false,'re-entry keeps the four-field draft editor visible');
    for(const key of ['good','priority','next_7_days','comment'])assert.equal(app.ids[key].disabled,false,`${key} remains writable for the next review version`);
  }
}

async function queueCuesAndAiSummaryStayDistinctFromHumanDraft() {
  const list = {items: [{case_id: 'case/internal-alpha', case_code: 'HC-7F3A91', status: 'ready_for_review', valid_day_count: 4, record_count: 6, collected_date_start: '2026-09-13', collected_date_end: '2026-09-15', window_started_at: '2026-09-13T00:00:00+08:00', window_ends_at: '2026-09-20T00:00:00+08:00', profile: {name: null, goal: null}}]};
  const aiDetail = {...detail, case_id: 'case/internal-alpha', valid_day_count: 4, valid_days: [...threeQualifiedDays, validDay('2026-09-05')], profile: {name: null, goal: null}, current_review_version: 9, source_logs: [{...detail.source_logs[0], local_date: '2026-09-13', normalized_meal_slot: 'lunch'}, {log_id: 'log C', food_log_version: 1, local_date: '2026-09-14', normalized_meal_slot: 'dinner', nutrition_snapshot: {calories_kcal: 520, protein_g: null}}], source_integrity: {referenced_count: 2, available_snapshot_count: 2, all_snapshots_available: true}, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 9, status: 'draft', ai_observations: {observation: '兩天都有正餐紀錄'}, review: {good: '人工草稿不可混入 AI', priority: '人工優先事項', next_7_days: '人工計畫', comment: '人工點評'}, suggested_values: {protein_g: 90}, limitations: '人工限制', created_at: '2026-09-15T01:00:00+08:00', updated_at: '2026-09-15T01:00:00+08:00', approved_at: null}};
  const app = await createApp(async path => path.includes('?') ? jsonResponse(list) : jsonResponse(aiDetail), {autoOpen: false});
  const row = find(app.cases, el => el.className === 'case-row');
  const queueText = renderedText(row);
  assert.ok(queueText.includes('HC-7F3A91'));
  assert.ok(queueText.includes('6 筆紀錄'));
  assert.ok(queueText.includes('2026/09/13–2026/09/15'));
  assert.ok(queueText.includes('已達標 4 天／門檻 3 天'));
  assert.ok(!queueText.includes('case/internal-alpha'));
  await row.dispatch('click');
  const aiText = renderedText(app.ids.latestReview);
  assert.ok(aiText.includes('資料來源'));
  assert.ok(aiText.includes('2 筆可驗證營養快照'));
  assert.ok(aiText.includes('營養計算'));
  assert.ok(aiText.includes('1,200 kcal'));
  assert.ok(aiText.includes('蛋白質資料不足'));
  assert.ok(aiText.includes('整體重點'));
  assert.ok(aiText.includes('兩天都有正餐紀錄'));
  for (const forbidden of ['人工草稿不可混入 AI', '人工優先事項', '人工計畫', '人工點評', '審核版本', 'schema', 'hash']) assert.ok(!aiText.includes(forbidden), forbidden);
}

async function systemInitialDraftRequiresExplicitApplyAndNeverOverwrites() {
  const token = '9'.repeat(64);
  const seed = {
    good: '已記錄 3 個符合完整度規則的日期。',
    priority: '資料限制與優先事項仍需由營養師確認。',
    next_7_days: '由營養師依資料與顧客目標完成接下來 7 天內容。',
    comment: '',
  };
  const summary = {
    kind: 'system_data_summary_v1', display_label: '系統初稿（未儲存，待營養師審核）',
    persistence: 'not_saved', customer_visible: false, delivery_eligible: false,
    requires_dietitian_review: true, may_generate_preview: true,
    source_binding: {source_token: token, review_version: 0},
    coverage: {observed_day_count: 3, qualified_day_count: 3, source_count: 6},
    nutrition: {calories_kcal: {qualified_day_average: 1000, qualified_days_with_complete_value: 3, unit: 'kcal'}},
    targets: {calories_kcal: 2100, protein_g: null},
    observations: ['3 個符合完整度規則的日期，共有 6 筆可驗證營養快照。'],
    limitations: ['蛋白質目標未提供，不進行達標率或缺口判讀。'], editable_seed: seed,
  };
  const fresh = {...detail, source_token: token, current_review_version: 0, latest_review: null, latest_review_available: false, latest_review_fresh: null, system_initial_draft: summary};
  const calls = [];
  const app = await createApp(async (path, options = {}) => { calls.push({path, options}); return path.includes('?') ? jsonResponse(listing) : jsonResponse(fresh); }, {autoOpen: false});
  await find(app.cases, el => el.className === 'case-row').dispatch('click');
  assert.ok(renderedText(app.ids.latestReview).includes('系統初稿（未儲存，待營養師審核）'));
  assert.ok(renderedText(app.ids.latestReview).includes('符合記錄門檻日的已記錄量日均 1,000 kcal（3 個欄位完整日期）'));
  assert.ok(renderedText(app.ids.latestReview).includes('每日目標 2,100 kcal'));
  assert.equal(app.ids.applySystemDraft.disabled, false);
  await app.ids.applySystemDraft.dispatch('click');
  assert.equal(app.ids.good.value, seed.good);
  assert.equal(app.ids.priority.value, seed.priority);
  assert.equal(app.ids.next_7_days.value, seed.next_7_days);
  assert.equal(app.ids.comment.value, '', 'personal comment remains human-authored');
  assert.ok(app.ids.draftState.textContent.includes('未儲存'));
  assert.equal(app.ids.applySystemDraft.disabled, true);
  assert.ok(app.ids.systemDraftHint.textContent.includes('不覆蓋'));
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, 'apply is local dirty state, never auto-save');

  const human = {good: '人工既有', priority: '人工優先', next_7_days: '人工七天', comment: '人工點評'};
  const reviewed = {...fresh, current_review_version: 1, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 1, status: 'draft', review: human, limitations: '人工限制'}, system_initial_draft: {kind: 'existing_review_preserved', may_generate_preview: false}};
  const existingApp = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(reviewed), {autoOpen: false});
  await find(existingApp.cases, el => el.className === 'case-row').dispatch('click');
  assert.equal(existingApp.ids.applySystemDraft.disabled, true);
  await existingApp.ids.applySystemDraft.dispatch('click');
  assert.equal(existingApp.ids.good.value, human.good, 'saved human review is never overwritten');
}

(async () => {
  await systemInitialDraftRequiresExplicitApplyAndNeverOverwrites();
  await queueCuesAndAiSummaryStayDistinctFromHumanDraft();
  await supplementNotificationPairingAndResolvedReadback();
  await supplementOnlyDirtyNavigationMatrix();
  await supplementLateResponseOwnershipMatrix();
  await supplementRequestHttpContractAndLifecycleMatrix();
  await draftHappyPathPostsExactFieldsAndReloads();
  await formalPreviewMatchesPersistedReportAndActualLineLiterally();
  await invalidListPayloadFailsClosedWithoutTrustedZeroes();
  await draftFailureDirtyAndLateResponseMatrix();
  await approvalClickFlowContractAndLifecycleMatrix();
  await outcomeUnknownRequiresManualReconciliationWithoutResend();
  await approvalReadbackProjectionMismatchMatrixFailsClosed();
  await approvalLateCompletionAfterAppGenerationRefreshCannotPaint();
  await terminalApprovalClickNeverPosts();
  await cancelledApprovalConfirmationNeverPosts();
  await approvalReadbackRendersUntrustedTextLiterally();
  await missingOrMalformedSourceTokenKeepsDraftReadOnly();
  await queueRowsHaveOneCtaAndFourGridItems();
  await verifiedApiReviewProjectionIsRenderedInChinese();
  await queueWaitingCopyMatchesDeliveryState();
  await liveQueueAndDetailNavigation();
  await oneBrokenDetailDoesNotEraseQueue();
  await hungDetailCannotBlockQueueOrHealthyCase();
  await newerDetailStatusControlsDetailAndPhotos();
  await boundedFetchAndJsonTimeoutsUseAbortAndClearTimers();
  await reverseReloadCannotOverwriteNewQueue();
  await reverseReloadDetailCannotOverwriteNewSnapshot();
  await lateDetailCannotOverwriteAnotherOpenCase();
  await invalidDetailIdentityOrStatusFailsClosed();
  await happyPath();
  await zeroNutritionValuesRemainRecordedValues();
  await verifiedTimelineGroupsDatesSlotsAndKeepsLegacy();
  await mealSlotsUseCanonicalPresentationOrderWithoutDroppingUnknowns();
  await duplicateIdentityPrefersWholeBoundSourceRegardlessOfOrder();
  await duplicateConflictMatrixFailsClosedWithoutSyntheticCardsOrPhotos();
  await switchingTimelineDateBlocksLatePhoto();
  await chineseCardsAndPhotoStatusCopy();
  await compactPeriodAndMealCardPresentation();
  await taipeiTimestampRejectsNormalizedDateTimeFields();
  await staleRequestCannotAttach();
  await reloadRevokesLoadedPhoto();
  await switchingCasesRevokesPriorCasePhotos();
  await latePriorCaseResponseCannotAttach();
  await cancelledCaseRequestCanRetryWithoutOldFinallyUnlockingIt();
  await pagehideRevokes();
  await errorControls();
  await sourceIntegrityCopy();
  await invalidSourceLogsNeverMasqueradeAsZeroRecords();
  await invalidSourceCountsNeverFallBackToRenderedLogLength();
  await validDayCountRequiresConsistentDayRecords();
  console.log('dietitian LIFF photo behavior: PASS');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
