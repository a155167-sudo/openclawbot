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
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() {}
  async dispatch(type) {
    for (const handler of this.listeners[type] || []) await handler({type, target: this});
    await settle();
  }
}

function walk(root) {
  return [root, ...root.children.flatMap(child => child instanceof Element ? walk(child) : [])];
}
function find(root, predicate) { return walk(root).find(predicate); }
function renderedText(root) { return root.textContent + root.children.map(child => child instanceof Element ? renderedText(child) : String(child)).join(''); }
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

async function createApp(fetchImpl, {autoOpen = true, clock = null} = {}) {
  const status = new Element('p');
  const cases = new Element('section');
  const refresh = new Element('button');
  const ids = {status, cases, refresh};
  for (const id of ['queue','review','filters','search','caseList','queueCount','backQueue','caseName','caseSubtitle','caseStatus','profile','limitations','validDays','sourceIntegrity','timelineTabs','timelineDays','legacySources','sourceLogs','latestReview','reviewGrid','evidenceTab','reviewTab','draftEditor','draftPreview','draftPreviewContent','draftState','draftError','good','priority','next_7_days','comment','saveDraft','openDraftPreview','backDraftEdit']) ids[id] = new Element(id === 'search' ? 'input' : ['good','priority','next_7_days','comment'].includes(id) ? 'textarea' : id.endsWith('Tab') || ['backQueue','saveDraft','openDraftPreview','backDraftEdit'].includes(id) ? 'button' : 'div');
  ids.caseList = cases;
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
      confirm() { return true; },
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
    return jsonResponse({...original, current_review_version: 1, latest_review_available: true, latest_review_fresh: true, latest_review: {review_version: 1, status: 'draft', review: fields, ai_observations: {}, suggested_values: {}, limitations: '', created_at: '2026-09-04T01:00:00+08:00', updated_at: '2026-09-04T01:00:00+08:00'}});
  }, {autoOpen: false});
  await find(app.cases, el => el.className === 'case-row').dispatch('click');
  for (const [key, value] of Object.entries(fields)) { app.ids[key].value = value; await app.ids[key].dispatch('input'); }
  assert.ok(app.ids.draftState.textContent.includes('未儲存'));
  await app.ids.openDraftPreview.dispatch('click');
  assert.equal(app.ids.draftPreview.hidden, false);
  assert.ok(renderedText(app.ids.draftPreviewContent).includes('<b>一步一步來</b>'), 'preview uses text, not markup');
  await app.ids.backDraftEdit.dispatch('click');
  await app.ids.saveDraft.dispatch('click');
  const post = calls.find(call => call.options.method === 'POST');
  assert.deepEqual(JSON.parse(post.options.body), {...fields, expected_source_token: original.source_token, expected_review_version: 0, request_id: 'request-uuid-1'});
  assert.equal(post.options.headers.Authorization, 'Bearer memory-only-token');
  assert.equal(post.options.credentials, 'omit');
  assert.equal(app.ids.draftState.textContent, '草稿已儲存');
  assert.equal(app.ids.good.value, fields.good, 'GET reload preserves the saved fields');
  assert.equal(calls.filter(call => call.path.endsWith('/case%2FA') && call.options.method !== 'POST').length, 2, 'success is accepted only after a real detail reload');
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
    assert.ok(app.ids.draftError.textContent.includes('人工比較'));
    assert.equal(app.ids.good.value, values.good, '409 preserves all fields');
    const firstBody = calls.find(call => call.options.method === 'POST').options.body;
    postStatus = 503;
    await app.ids.saveDraft.dispatch('click');
    const postBodies = calls.filter(call => call.options.method === 'POST').map(call => call.options.body);
    assert.equal(postBodies[1], firstBody, 'retry of unchanged unknown/conflict payload retains request id and exact body');
    assert.ok(app.ids.draftError.textContent.includes('同一請求重試'));
    app.ids.comment.value = '新的點評'; await app.ids.comment.dispatch('input');
    await app.ids.saveDraft.dispatch('click');
    const changedBody = JSON.parse(calls.filter(call => call.options.method === 'POST')[2].options.body);
    assert.equal(changedBody.request_id, 'request-uuid-2', 'changed payload receives a new request id');
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
  for (const expected of [
    '審核狀態：已核准', '核准時間：2026/09/05 08:30', '資料限制：資料僅涵蓋三個有效日',
    'AI 觀察：蛋白質分布可再平均', 'AI 觀察列表：晚餐占比較高', 'AI 優勢：有記錄飲水',
    'AI 缺口：蔬菜不足', 'AI 風險：鈉偏高', '做得好的地方：記錄完整',
    '審核優勢：早餐穩定、蛋白質來源多元', '改善項目：午餐補一份蔬菜',
    '建議：每日飲水 2000 ml', '建議熱量：1900 kcal', '建議飲水：2000 ml',
  ]) assert.ok(text.includes(expected), expected);
  for (const forbidden of ['review-2', apiFixture.source_manifest_hash, '/srv/', 'activation-secret']) {
    if (forbidden) assert.ok(!text.includes(forbidden), `must not render raw identifier/path/hash: ${forbidden}`);
  }

  const stale = {...apiFixture, latest_review_fresh: false, latest_review_available: false};
  const staleApp = await createApp(async path => path.includes('?') ? jsonResponse(fixtureListing) : jsonResponse(stale));
  assert.ok(!renderedText(staleApp.ids.latestReview).includes('記錄完整'), 'stale review payload is not consumed');

  const malformed = {...apiFixture, latest_review: {...apiFixture.latest_review, review: {strengths: {raw: 'secret-path'}, improvements: [7], recommendations: null}, ai_observations: []}};
  const malformedApp = await createApp(async path => path.includes('?') ? jsonResponse(fixtureListing) : jsonResponse(malformed));
  const malformedText = renderedText(malformedApp.ids.latestReview);
  assert.ok(malformedText.includes('AI 審核觀察：NA'));
  assert.ok(malformedText.includes('審核優勢：NA'));
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
  const rows = walk(app.cases).filter(el => el.className === 'case-row');
  const copy = Object.fromEntries(rows.map((row, index) => [queueListing.items[index].case_id, renderedText(row)]));
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
  assert.ok(text.includes('未提供'));
  assert.ok(text.includes('待投遞'));
  assert.ok(text.includes('已送達'));
  assert.ok(!text.includes('共 3 案'), 'limited page must not claim a global count');
  assert.ok(text.includes('已送出，等待處理'));

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
  assert.ok(detailText.includes('尚無可用的最新審核'));
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
  assert.ok(text.includes('脂肪：NA'));
  assert.ok(text.includes('碳水化合物：NA'));
  assert.ok(text.includes('餐點紀錄'));
  assert.ok(text.includes('日期／餐別尚無可驗證資料'));
  assert.ok(text.includes('膳食纖維：NA'));
  assert.ok(text.includes('鈉：NA'));
  assert.ok(text.includes('非營養師核准'));
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

  const close = find(app.cases, el => el.tagName === 'BUTTON' && el.textContent === '關閉照片');
  await close.dispatch('click');
  assert.deepEqual(app.revoked, ['blob:test-1']);
  assert.equal(find(app.cases, el => el.tagName === 'IMG'), undefined);
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
  assert.deepEqual(tabs.map(tab => tab.textContent), ['2026-09-02', '2026-09-03', '2026-09-05', '2026-09-06']);
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
  assert.ok(!renderedText(app.ids.timelineTabs).includes('2026-09-04'), 'single missing binding field does not borrow a date');
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
    assert.ok(renderedText(app.ids.timelineTabs).includes('2026-09-02'), `${name}: bound source remains on timeline`);
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
  const technical = find(app.cases, el => el.tagName === 'DETAILS' && renderedText(el).includes('技術資料'));
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
    const technical = find(card, el => el.tagName === 'DETAILS' && renderedText(el).includes('技術資料'));
    assert.ok(technical);
    assert.equal(technical.attributes.open, undefined, 'technical details are collapsed by default');
    const summary = find(technical, el => el.tagName === 'SUMMARY' && el.textContent === '技術資料');
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

(async () => {
  await draftHappyPathPostsExactFieldsAndReloads();
  await draftFailureDirtyAndLateResponseMatrix();
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
