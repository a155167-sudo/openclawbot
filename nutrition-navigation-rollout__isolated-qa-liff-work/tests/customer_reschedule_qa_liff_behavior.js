const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(match => match[1]);
const script = scripts.find(source => source.includes('(function(){'));

class Element {
  constructor(tag = 'div') {
    this.tag = tag;
    this.textContent = '';
    this.children = [];
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.options = [];
    this.dataset = {};
    this.onclick = null;
    this.onchange = null;
    this._innerHTML = '';
    this.classList = {
      values: new Set(['hidden']),
      remove: value => this.classList.values.delete(value),
      add: value => this.classList.values.add(value),
      contains: value => this.classList.values.has(value),
    };
  }
  set innerHTML(value) {
    this._innerHTML = value;
    this.children = [];
    this.options = [];
    this.value = '';
    this.textContent = '';
  }
  get innerHTML() {
    return this._innerHTML;
  }
  add(option) {
    this.options.push(option);
    if (!this.value) this.value = option.value;
  }
  append(child) {
    this.children.push(child);
  }
  async dispatch(type) {
    const handler = type === 'click' ? this.onclick : this.onchange;
    if (handler) await handler.call(this, {type});
  }
}

class Option {
  constructor(text, value) {
    this.text = text;
    this.textContent = text;
    this.value = value;
    this.dataset = {};
  }
}

function response(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return {promise, resolve, reject};
}

function makeClock() {
  const timers = [];
  return {
    timers,
    setTimeout(fn, delay) {
      timers.push({fn, delay});
      return timers.length;
    },
    clearTimeout() {},
    runAll() {
      for (const timer of [...timers]) timer.fn();
    },
  };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
}

async function app(fetchImpl, {clock = null, uuidPrefix = 'uuid'} = {}) {
  const ids = {};
  for (const id of ['source', 'target', 'submit', 'contextStatus', 'preview', 'result', 'adminPanel', 'refresh', 'pending']) {
    ids[id] = new Element(id === 'source' || id === 'target' ? 'select' : id === 'submit' || id === 'refresh' ? 'button' : 'div');
  }
  const cryptoImpl = {randomUUID: (() => {
    let counter = 0;
    return () => `${uuidPrefix}-${++counter}`;
  })()};
  const ctx = {
    console,
    window: {__RESCHEDULE_RUNTIME__: {liffId: '1234567890-isolated'}, crypto: cryptoImpl},
    document: {
      getElementById: id => ids[id],
      createElement: tag => new Element(tag),
    },
    Option,
    fetch: fetchImpl,
    AbortController,
    setTimeout: clock ? clock.setTimeout : setTimeout,
    clearTimeout: clock ? clock.clearTimeout : clearTimeout,
    crypto: cryptoImpl,
    liff: {
      init: async () => {},
      isLoggedIn: () => true,
      getIDToken: () => 'tok-owner',
      login() {},
    },
  };
  vm.runInNewContext(script, ctx);
  await settle();
  return {ids, ctx};
}

const contextPayload = {
  orders: [{
    order_id: 1,
    source_dates: [{date: '2026-09-27', label: '第1週-日', meals: [{meal: '午餐', label: '午餐A'}]}],
    target_dates: ['2026-09-28', '2026-09-29'],
  }],
};

async function timeoutUsesAbortController() {
  const clock = makeClock();
  const gate = deferred();
  const appState = await app((url, options = {}) => {
    assert.equal(url, '/customer-reschedule/context');
    assert.equal(clock.timers[0]?.delay, 40000);
    options.signal.addEventListener('abort', () => {
      const err = new Error('aborted');
      err.name = 'AbortError';
      gate.reject(err);
    });
    return gate.promise;
  }, {clock});
  clock.runAll();
  await settle();
  assert.match(appState.ids.result.textContent, /載入失敗：連線逾時/);
}

async function unchangedSubmitReusesRequestIdAndChangedIntentGetsNewOne() {
  const posts = [];
  let firstPost = true;
  const appState = await app(async (url, options = {}) => {
    if (url === '/customer-reschedule/context') return response(200, contextPayload);
    if (String(url).startsWith('/customer-reschedule/preview')) return response(200, {
      source_date: '2026-09-27',
      target_date: '2026-09-28',
      source_meals: [{meal: '午餐', label: '午餐A'}],
    });
    if (url === '/api/admin/customer-pair-reschedule-requests') return response(403, {detail: 'forbidden'});
    if (url === '/customer-reschedule/pending-request') {
      posts.push(JSON.parse(options.body));
      if (firstPost) {
        firstPost = false;
        throw new Error('lost response');
      }
      return response(200, {request_id: posts[posts.length - 1].request_id, status: 'pending_admin'});
    }
    throw new Error(`unexpected ${url}`);
  });
  await settle();
  await appState.ids.submit.dispatch('click');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].request_id, 'qa-uuid-1');
  assert.match(appState.ids.result.textContent, /不會自動重送/);
  await appState.ids.submit.dispatch('click');
  assert.equal(posts.length, 2);
  assert.equal(posts[1].request_id, 'qa-uuid-1');
  appState.ids.target.value = '2026-09-29';
  await appState.ids.target.dispatch('change');
  await appState.ids.submit.dispatch('click');
  assert.equal(posts.length, 3);
  assert.equal(posts[2].request_id, 'qa-uuid-2');
}

async function operationResultSurvivesForbiddenAndEmptyContextRefresh() {
  for (const afterContext of [
    response(403, {detail: 'forbidden'}),
    response(200, {orders: [], detail: '目前沒有可改期的來源日期。'}),
  ]) {
    let contextCalls = 0;
    const appState = await app(async (url, options = {}) => {
      if (url === '/customer-reschedule/context') {
        contextCalls += 1;
        return contextCalls === 1 ? response(200, contextPayload) : afterContext;
      }
      if (String(url).startsWith('/customer-reschedule/preview')) return response(200, {
        source_date: '2026-09-27',
        target_date: '2026-09-28',
        source_meals: [{meal: '午餐', label: '午餐A'}],
      });
      if (url === '/api/admin/customer-pair-reschedule-requests') return response(200, {
        requests: [{request_id: 'r1', source_date: '2026-09-27', target_date: '2026-09-28', status: 'pending_admin', can_approve: true}],
      });
      if (url === '/api/admin/customer-pair-reschedule-requests/r1/approve') return response(200, {
        status: 'confirmed',
      });
      throw new Error(`unexpected ${url} ${options.method || 'GET'}`);
    });
    await settle();
    const approveButton = appState.ids.pending.children[0].children[1];
    await approveButton.dispatch('click');
    assert.equal(appState.ids.result.textContent, '核准結果：已確認完成。');
  }
}

async function emptyEligibleTargetsDisableSubmitWithoutPost() {
  let postCount = 0;
  const appState = await app(async (url, options = {}) => {
    if (url === '/customer-reschedule/context') return response(200, {
      orders: [{
        order_id: 1,
        source_dates: [{date: '2026-10-07', label: '權益最後日', meals: [{meal: '午餐', label: '午餐A'}]}],
        target_dates: [],
      }],
    });
    if (url === '/api/admin/customer-pair-reschedule-requests') return response(403, {detail: 'forbidden'});
    if (url === '/customer-reschedule/pending-request') {
      postCount += 1;
      return response(200, {});
    }
    throw new Error(`unexpected ${url} ${options.method || 'GET'}`);
  });
  await settle();
  assert.equal(appState.ids.submit.disabled, true);
  assert.match(appState.ids.contextStatus.textContent, /沒有有效權益內可送出且可核准的目標日期/);
  await appState.ids.submit.dispatch('click');
  assert.equal(postCount, 0);
}

async function adminButtonsHonorServerActionFlags() {
  const appState = await app(async (url, options = {}) => {
    if (url === '/customer-reschedule/context') return response(403, {detail: 'forbidden'});
    if (url === '/api/admin/customer-pair-reschedule-requests') return response(200, {
      requests: [{
        request_id: 'expired-1',
        source_date: '2026-09-27',
        target_date: '2026-09-28',
        status: 'pending_admin',
        can_approve: false,
        can_reconcile: false,
        disabled_reason: '申請已逾期，請顧客重新送出申請。',
      }],
    });
    if (String(url).includes('/approve')) throw new Error('expired row must not approve');
    throw new Error(`unexpected ${url} ${options.method || 'GET'}`);
  });
  await settle();
  const row = appState.ids.pending.children[0];
  assert.match(row.children[0].textContent, /申請已逾期，請顧客重新送出申請/);
  assert.equal(row.children.length, 1);
}

(async () => {
  assert.ok(script, 'expected emitted page script');
  await timeoutUsesAbortController();
  await unchangedSubmitReusesRequestIdAndChangedIntentGetsNewOne();
  await operationResultSurvivesForbiddenAndEmptyContextRefresh();
  await emptyEligibleTargetsDisableSubmitWithoutPost();
  await adminButtonsHonorServerActionFlags();
})().catch(error => {
  console.error(error);
  process.exit(1);
});
