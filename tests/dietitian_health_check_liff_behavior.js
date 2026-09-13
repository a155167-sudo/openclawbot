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
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  async dispatch(type) {
    for (const handler of this.listeners[type] || []) await handler({type, target: this});
    await settle();
  }
}

function walk(root) {
  return [root, ...root.children.flatMap(child => child instanceof Element ? walk(child) : [])];
}
function find(root, predicate) { return walk(root).find(predicate); }
function renderedText(root) { return walk(root).map(element => element.textContent).join('\n'); }
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

async function createApp(fetchImpl) {
  const status = new Element('p');
  const cases = new Element('section');
  const refresh = new Element('button');
  const ids = {status, cases, refresh};
  const objectUrls = [];
  const revoked = [];
  const windowListeners = {};
  const context = {
    console,
    setTimeout,
    clearTimeout,
    AbortController,
    document: {
      getElementById: id => ids[id],
      createElement: tag => new Element(tag),
    },
    location: {href: 'https://example.test/dietitian-health-check'},
    window: {
      addEventListener(type, handler) { (windowListeners[type] ||= []).push(handler); },
    },
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
  return {status, cases, refresh, objectUrls, revoked, windowListeners};
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
  assert.ok(text.includes('脂肪 NA｜碳水 NA'));
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
    return jsonResponse({valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: [{log_id: `log-${caseId}`, trust_type: 'user_confirmed_ai_estimate', estimate: {}}]});
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
    return Promise.resolve(jsonResponse({valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: [{log_id: `log-${caseId}`, trust_type: 'user_confirmed_ai_estimate', estimate: {}}]}));
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
    return Promise.resolve(jsonResponse({valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: [{log_id: `log-${caseId}`, trust_type: 'user_confirmed_ai_estimate', estimate: {}}]}));
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
    const app = await createApp(async path => path.includes('?') ? jsonResponse(caseListing) : jsonResponse(scenario.detail));
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
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(body));
    const text = renderedText(app.cases);
    assert.ok(text.includes('保留來源參照：資料不可用'), name);
    assert.ok(text.includes('目前可驗證快照：資料不可用'), name);
    assert.ok(text.includes('來源快照清單資料不可用'), name);
    assert.ok(!text.includes('保留來源參照：0 筆'), `${name}: malformed records must not prove zero references`);
    assert.ok(!text.includes('目前可驗證快照：0 筆'), `${name}: malformed records must not prove zero snapshots`);
  }

  {
    const body = {valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: [], source_integrity: {referenced_count: 0, available_snapshot_count: 0, all_snapshots_available: true}};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(body));
    const text = renderedText(app.cases);
    assert.ok(text.includes('保留來源參照：0 筆'));
    assert.ok(text.includes('目前可驗證快照：0 筆'));
    assert.ok(!text.includes('來源快照清單資料不可用'));
  }

  {
    const body = {valid_day_count: 3, valid_days: threeQualifiedDays, source_logs: null, source_integrity: {referenced_count: 6, available_snapshot_count: 0, all_snapshots_available: false}};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(body));
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
    const app = await createApp(async path => path.includes('?') ? jsonResponse(listing) : jsonResponse(body));
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
    const app = await createApp(async path => path.includes('?') ? jsonResponse(caseListing) : jsonResponse(body));
    const text = renderedText(app.cases);
    assert.ok(text.includes('有效日：資料不可用'), name);
    assert.ok(!text.includes('有效日：3 / 3'), `${name}: must not trust list fallback`);
  }
}

async function validDayCountRequiresConsistentDayRecords() {
  const validCases = [
    ['three qualified days', 3, threeQualifiedDays, ['有效日：3 / 3', '2026-09-02｜2 餐｜qualified', '2026-09-04｜2 餐｜qualified']],
    ['zero days', 0, [], ['有效日：0 / 3', '目前尚無可列入的日期']],
    ['incomplete day does not count', 0, [validDay('2026-09-02', 'incomplete', 1)], ['有效日：0 / 3', '2026-09-02｜1 餐｜incomplete']],
  ];
  for (const [name, count, validDays, expectedTexts] of validCases) {
    const caseListing = {items: [{...listing.items[0], valid_day_count: count}]};
    const body = {valid_day_count: count, valid_days: validDays, source_logs: []};
    const app = await createApp(async path => path.includes('?') ? jsonResponse(caseListing) : jsonResponse(body));
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
    const app = await createApp(async path => path.includes('?') ? jsonResponse(caseListing) : jsonResponse(body));
    const text = renderedText(app.cases);
    assert.ok(text.includes('有效日：資料不可用'), name);
    assert.ok(!text.includes(`有效日：${count} / 3`), `${name}: contradictory count must not render`);
  }
}

(async () => {
  await happyPath();
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
