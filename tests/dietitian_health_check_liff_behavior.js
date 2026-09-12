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
const detail = {valid_days: [], source_logs: [{log_id: 'log B', trust_type: 'user_confirmed_ai_estimate', estimate: {}}]};

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
    return jsonResponse({valid_days: [], source_logs: [{log_id: `log-${caseId}`, trust_type: 'user_confirmed_ai_estimate', estimate: {}}]});
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
    return Promise.resolve(jsonResponse({valid_days: [], source_logs: [{log_id: `log-${caseId}`, trust_type: 'user_confirmed_ai_estimate', estimate: {}}]}));
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
    return Promise.resolve(jsonResponse({valid_days: [], source_logs: [{log_id: `log-${caseId}`, trust_type: 'user_confirmed_ai_estimate', estimate: {}}]}));
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

(async () => {
  await happyPath();
  await staleRequestCannotAttach();
  await reloadRevokesLoadedPhoto();
  await switchingCasesRevokesPriorCasePhotos();
  await latePriorCaseResponseCannotAttach();
  await cancelledCaseRequestCanRetryWithoutOldFinallyUnlockingIt();
  await pagehideRevokes();
  await errorControls();
  console.log('dietitian LIFF photo behavior: PASS');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
