/* Behavioral checks against the real browser script; no device or network access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function browser() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      id, textContent: '', innerHTML: '', hidden: false, disabled: false,
      value: '', dataset: {}, style: {}, listeners: {}, children: [],
      classList: {
        values: new Set(), add(x) { this.values.add(x); }, remove(x) { this.values.delete(x); },
        toggle(x, on) { if (on) this.values.add(x); else this.values.delete(x); },
        contains(x) { return this.values.has(x); },
      },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      querySelector(selector) { return element(`${id}/${selector}`); },
      querySelectorAll(selector) {
        if (selector === '.system-detail-card') return Array.from({length: 6}, (_, i) => element(`${id}/card${i}`));
        if (selector === 'dd') return Array.from({length: 3}, (_, i) => element(`${id}/dd${i}`));
        return [];
      },
      focus() {}, remove() {}, scrollIntoView() {}, append(child) { this.children.push(child); },
      requestSubmit() { return this.listeners.submit?.({preventDefault() {}, currentTarget: this}); },
      replaceChildren(...children) { this.children = children; },
      insertAdjacentHTML(position, html) { this.innerHTML += html; },
    });
    return elements.get(id);
  }
  let reply = () => new Promise(() => {}); // Startup fetches remain pending in this isolated DOM.
  const storage = new Map();
  const requests = [];
  const timers = new Map();
  const listeners = {};
  const context = {
    URL, Headers, AbortSignal, console,
    document: {
      getElementById: element, querySelector: selector => selector.startsWith('meta') ? {content: 'csrf-test'} : element(selector),
      querySelectorAll: () => [], documentElement: {dataset: {page: 'system'}},
      createElement: tag => element('new/' + tag),
    },
    window: {
      location: {href: 'http://localhost/', origin: 'http://localhost', assign() {}},
      fetch: (url, init) => { requests.push({url, init}); return reply(url, init); },
      addEventListener: (name, callback) => { listeners[name] = callback; },
      setTimeout: (callback, delay) => { const id = timers.size + 1; timers.set(id, {callback, delay}); return id; },
      clearTimeout: id => timers.delete(id), setInterval() {},
    },
    sessionStorage: {
      getItem: key => storage.get(key) || '', setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
  };
  context.fetch = (...args) => context.window.fetch(...args);
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), context);
  const run = code => vm.runInContext(code, context);
  run('appStatus = {mode: "serial", ready: true}; cliAvailable = true');
  return {
    element, requests, timers, storage, listeners, run,
    respond(data, status = 200) { reply = async () => ({ok: status >= 200 && status < 300, status, json: async () => data}); },
    reject() { reply = async () => { throw new Error('simulated network failure'); }; },
    seedToken() { run('cliToken = "session-token"; cliConnected = true'); storage.set('qm_cli_token', 'session-token'); },
  };
}

const observed = '2026-11-17T10:00:00Z';
function snapshot() {
  return {
    name: 'Switch test', version: 'Test firmware', model: 'Test model', collected_at: observed,
    expected_ports: 1, inventory_stale: false,
    ports: [{number: 1, interface: 'Ethernet1/0/1', state: 'up', pvid: 12, mode: 'access',
      description: 'Test', rx_bps: 800, tx_bps: 400, stale: false, field_stale: {state: false, rx_bps: false, tx_bps: false},
      field_observed_at: {state: observed, rx_bps: observed, tx_bps: observed}, link_at: observed, collected_at: observed}],
    vlans: [{id: 12, name: 'Test', untagged_numbers: [1], tagged_numbers: [], collected_at: observed}],
    mac_table: {count: 1, entries: [{mac: '00:11:22:33:44:55', vlan: 12, port: 1}], collected_at: observed},
  };
}

test('diagnostic updates on content changes of equal length and escapes device text', async () => {
  const ui = browser();
  ui.respond({outputs: [{command: 'display vlan 12', output: 'Description: AAAA'}]});
  await ui.run('loadPhysicalCache()');
  ui.respond({outputs: [{command: 'display vlan 12', output: 'Description: BBBB'}]});
  await ui.run('loadPhysicalCache()');
  assert.match(ui.element('outputs').innerHTML, /BBBB/);
  assert.doesNotMatch(ui.element('outputs').innerHTML, /AAAA/);
  ui.respond({outputs: [{command: 'display vlan', output: '<script>bad</script>'}]});
  await ui.run('loadPhysicalCache()');
  assert.match(ui.element('outputs').innerHTML, /&lt;script&gt;/);
});

test('network loss marks old data offline and recovery redraws the same snapshot', async () => {
  const ui = browser();
  const data = snapshot();
  ui.respond({snapshot: data, stale: false, monitor: {state: 'ativo'}});
  await ui.run('loadPhysicalCache()');
  assert.equal(ui.element('connection-state').classList.contains('online'), true);
  ui.reject();
  await ui.run('loadPhysicalCache()');
  assert.equal(ui.element('connection-state').classList.contains('online'), false);
  assert.match(ui.element('port-grid').innerHTML, /ANT\./);
  assert.match(ui.element('physical-port-detail').textContent, /800 bit\/s \(anterior\)/);
  ui.respond({snapshot: data, stale: false, monitor: {state: 'ativo'}});
  await ui.run('loadPhysicalCache()');
  assert.equal(ui.element('connection-state').classList.contains('online'), true);
  assert.doesNotMatch(ui.element('port-grid').innerHTML, /ANT\./);
});

test('resource age changes redraw even without a new global collection time', async () => {
  const ui = browser();
  const data = snapshot();
  ui.respond({snapshot: data, stale: false, monitor: {state: 'ativo'}});
  await ui.run('loadPhysicalCache()');
  assert.match(ui.element('ports-count-note').textContent, /Todos os links/);
  const old = structuredClone(data);
  old.ports[0].field_stale.state = true;
  old.ports[0].field_stale.rx_bps = true;
  old.ports[0].collected_at = '2026-11-17T11:00:00Z';
  old.mac_table.stale = true;
  old.vlans[0].membership_stale = true;
  ui.respond({snapshot: old, stale: false, monitor: {state: 'ativo'}});
  await ui.run('loadPhysicalCache()');
  assert.match(ui.element('port-grid').innerHTML, /ANT\./);
  assert.match(ui.element('ports-count-note').textContent, /0 de 1/);
  assert.match(ui.element('physical-traffic').innerHTML, /800 \(anterior\)/);
  assert.match(ui.element('physical-traffic').innerHTML, new RegExp(new Date(observed).toLocaleTimeString('pt-BR')));
  assert.match(ui.element('switch-connections-note').textContent, /Tabela MAC anterior/);
  assert.match(ui.element('vlan-body').innerHTML, /Leitura anterior/);
});

test('failed close keeps ownership and retry closes only after server confirmation', async () => {
  const ui = browser();
  ui.seedToken();
  ui.reject();
  assert.equal(await ui.run('closeCli()'), false);
  assert.equal(ui.run('cliToken'), 'session-token');
  assert.equal(ui.storage.get('qm_cli_token'), 'session-token');
  assert.equal(ui.element('cli-close').disabled, false);
  assert.equal(ui.element('cli-send').disabled, true);
  ui.respond({ok: true, message: 'closed'});
  assert.equal(await ui.run('closeCli()'), true);
  assert.equal(ui.run('cliToken'), '');
  assert.equal(ui.storage.has('qm_cli_token'), false);
  assert.equal(ui.element('cli-close').disabled, true);
});

test('pagehide preserves token on missing close confirmation and recovers validated session', async () => {
  const ui = browser();
  ui.seedToken();
  ui.reject();
  ui.listeners.pagehide();
  await new Promise(setImmediate);
  assert.equal(ui.storage.get('qm_cli_token'), 'session-token');
  assert.equal(ui.requests.at(-1).init.keepalive, true);
  ui.respond({mode: 'serial', available: true, connected: true, token_valid: true});
  await ui.run('loadCliStatus()');
  assert.equal(ui.run('cliConnected && !cliStateUncertain'), true);
  assert.equal(ui.requests.at(-1).init.headers['X-CLI-Token'], 'session-token');
});

test('CLI busy status is inconclusive and does not discard recovery token', async () => {
  const ui = browser();
  ui.seedToken();
  ui.respond({mode: 'serial', available: true, connected: true, busy: true, token_valid: null});
  await ui.run('loadCliStatus()');
  assert.equal(ui.run('cliToken'), 'session-token');
  assert.equal(ui.element('cli-send').disabled, true);
  assert.equal(ui.element('cli-close').disabled, false);
  assert.equal(ui.timers.size, 1);
  ui.respond({mode: 'serial', available: true, connected: true, token_valid: true});
  await ui.run('loadCliStatus()');
  assert.equal(ui.element('cli-send').disabled, false);
  assert.equal(ui.timers.size, 0);
});

test('server-confirmed closed or different session clears stale token without taking ownership', async () => {
  for (const connected of [false, true]) {
    const ui = browser();
    ui.seedToken();
    ui.respond({mode: 'serial', available: true, connected, token_valid: false});
    await ui.run('loadCliStatus()');
    assert.equal(ui.run('cliToken'), '');
    assert.equal(ui.element('cli-open').disabled, connected);
    assert.equal(ui.element('cli-send').disabled, true);
  }
});


test('VLAN table uses temporally reconciled memberships instead of historical lists', async () => {
  const ui = browser();
  const data = snapshot();
  data.vlans[0].untagged_numbers = [1, 2];
  data.vlans[0].effective_tagged_numbers = [];
  data.vlans[0].effective_untagged_numbers = [2];
  ui.respond({snapshot: data, stale: false, monitor: {state: 'ativo'}});
  await ui.run('loadPhysicalCache()');
  assert.match(ui.element('vlan-body').innerHTML, /<td>2<\/td>/);
  assert.doesNotMatch(ui.element('vlan-body').innerHTML, /1, 2/);
});


function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function preview(token, summary) {
  return {ok: true, token, summary, commands: [summary], warnings: []};
}

test('only the newest preview may replace the shown operation, including old errors', async () => {
  for (const oldFails of [false, true]) {
    const ui = browser();
    ui.run('configStatus = {available: true, writes_enabled: true}');
    const old = deferred();
    ui.respond(old.promise);
    const oldPreview = ui.run('previewConfiguration({action: "create_vlan", vlan_id: 10})');
    ui.respond(preview('latest', 'Delete VLAN 20'));
    await ui.run('previewConfiguration({action: "delete_vlan", vlan_id: 20})');
    assert.equal(ui.run('pendingConfigPreview.token'), 'latest');
    if (oldFails) old.reject(new Error('outdated error'));
    else old.resolve(preview('old', 'Create VLAN 10'));
    await oldPreview;
    assert.equal(ui.run('pendingConfigPreview.token'), 'latest');
    assert.equal(ui.element('preview-summary').textContent, 'Delete VLAN 20');
    assert.equal(ui.element('apply-config-button').disabled, false);
    assert.doesNotMatch(ui.element('config-result').textContent, /outdated/);
  }
});

test('a failed latest preview cannot be replaced by an older successful response', async () => {
  const ui = browser();
  ui.run('configStatus = {available: true, writes_enabled: true}');
  const old = deferred();
  ui.respond(old.promise);
  const oldPreview = ui.run('previewConfiguration({action: "create_vlan", vlan_id: 10})');
  ui.respond({ok: false, error: 'Latest operation rejected'}, 400);
  await ui.run('previewConfiguration({action: "delete_vlan", vlan_id: 20})');
  old.resolve(preview('old', 'Create VLAN 10'));
  await oldPreview;
  assert.equal(ui.run('pendingConfigPreview'), null);
  assert.equal(ui.element('config-preview').hidden, true);
  assert.equal(ui.element('apply-config-button').disabled, true);
  assert.equal(ui.element('config-result').textContent, 'Latest operation rejected');
});

test('applying has a single operation and rejects concurrent previews and repeated apply', async () => {
  const ui = browser();
  ui.run('configStatus = {available: true, writes_enabled: true}; pendingConfigPreview = {token: "apply-token"}');
  const response = deferred();
  ui.respond(response.promise);
  const applying = ui.element('apply-config-button').listeners.click();
  assert.equal(ui.run('configApplying'), true);
  assert.equal(ui.element('save-config-button').disabled, true);
  assert.equal(ui.element('vlan-form').querySelector('button[type="submit"]').disabled, true);
  await ui.run('previewConfiguration({action: "save"})');
  await ui.element('apply-config-button').listeners.click();
  assert.equal(ui.requests.filter(request => request.url === '/api/config/apply').length, 1);
  assert.equal(ui.requests.filter(request => request.url === '/api/config/preview').length, 0);
  response.resolve({ok: false, error: 'Verification failed'});
  await applying;
  assert.equal(ui.run('configApplying'), false);
  assert.equal(ui.element('config-result').textContent, 'Verification failed');
  assert.equal(ui.element('apply-config-button').disabled, true);
  assert.equal(ui.element('save-config-button').disabled, false);
  ui.respond(preview('next', 'Save'));
  await ui.run('previewConfiguration({action: "save"})');
  assert.equal(ui.run('pendingConfigPreview.token'), 'next');
});

test('pending CLI command cannot be submitted twice by form, Enter, or Send Enter', async () => {
  const ui = browser();
  ui.seedToken();
  ui.element('cli-command').value = 'display version';
  const response = deferred();
  ui.respond(response.promise);
  const sending = ui.element('cli-command-form').requestSubmit();
  assert.equal(ui.element('cli-command').disabled, true);
  assert.equal(ui.element('cli-enter').disabled, true);
  assert.equal(ui.element('cli-send').disabled, true);
  assert.equal(ui.element('cli-close').disabled, true);
  await ui.element('cli-command-form').requestSubmit();
  ui.element('cli-enter').listeners.click();
  ui.element('cli-command').listeners.keydown({key: 'Enter', shiftKey: false, preventDefault() {}});
  assert.equal(ui.element('cli-command').value, 'display version');
  const sent = ui.requests.filter(request => request.url === '/api/cli/send');
  assert.equal(sent.length, 1);
  assert.equal(JSON.parse(sent[0].init.body).command, 'display version');
  response.resolve({ok: true, output: 'Version test\n<Review>'});
  await sending;
  assert.equal(ui.run('cliSending'), false);
  assert.equal(ui.element('cli-command').disabled, false);
  assert.equal(ui.element('cli-enter').disabled, false);
  assert.equal(ui.element('cli-send').disabled, false);
  assert.equal(ui.requests.filter(request => request.url === '/api/cli/send').length, 1);
});

test('CLI failed send releases controls without automatically retrying the command', async () => {
  const ui = browser();
  ui.seedToken();
  ui.element('cli-command').value = 'display version';
  ui.reject();
  await ui.element('cli-command-form').requestSubmit();
  assert.equal(ui.run('cliSending'), false);
  assert.equal(ui.element('cli-send').disabled, false);
  assert.equal(ui.requests.filter(request => request.url === '/api/cli/send').length, 1);
});

test('old CLI status response cannot discard a token opened after the status request', async () => {
  for (const oldFails of [false, true]) {
    const ui = browser();
    const status = deferred();
    ui.respond(status.promise);
    const statusRequest = ui.run('loadCliStatus()');
    ui.respond({ok: true, token: 'new-token', output: '<Review>', port: 'fake', baud_rate: 9600});
    await ui.element('cli-open').listeners.click();
    assert.equal(ui.run('cliToken'), 'new-token');
    if (oldFails) status.reject(new Error('old status error'));
    else status.resolve({mode: 'serial', available: true, connected: false, token_valid: false});
    await statusRequest;
    assert.equal(ui.run('cliToken'), 'new-token');
    assert.equal(ui.run('cliConnected'), true);
    assert.equal(ui.run('cliStateUncertain'), false);
  }
});

test('old CLI status cannot resurrect a closed session', async () => {
  const ui = browser();
  ui.seedToken();
  const status = deferred();
  ui.respond(status.promise);
  const statusRequest = ui.run('loadCliStatus()');
  ui.respond({ok: true, message: 'closed'});
  await ui.run('closeCli()');
  status.resolve({mode: 'serial', available: true, connected: true, token_valid: true});
  await statusRequest;
  assert.equal(ui.run('cliToken'), '');
  assert.equal(ui.run('cliConnected'), false);
  assert.equal(ui.run('cliOccupiedElsewhere'), false);
});

test('every element the script looks up by id exists and system cards are never rebuilt', () => {
  const js = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  const present = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
  const missing = [...new Set([...js.matchAll(/\$\("([^"]+)"\)/g)].map(m => m[1]))].filter(id => !present.has(id));
  assert.deepEqual(missing, []);
  // Replacing a card's innerHTML removes the ids applyDevice fills (port list included).
  assert.doesNotMatch(js, /system-detail-card"\)\[\d\]\.innerHTML/);
});

test('detected device fills the port list with real interface names and the system page', () => {
  const ui = browser();
  ui.run('appStatus = {mode: "virtual", ready: true}');
  ui.run(`applyDevice({vendor: "Huawei", model: "Quidway S3026C", version: "VRP 3.10 Release 0035",
    profile_label: "VRP 3 legado", validated_writes: false, guided_config: true, monitoring: true,
    ports: ["Ethernet0/1", "Ethernet0/2", "GigabitEthernet1/1"], port_summary: "2 FE · 1 GE", fingerprint: "a"})`);
  const options = ui.element('config-port').innerHTML;
  assert.match(options, /value="1">Porta 1 · Ethernet0\/1</);
  assert.match(options, /value="3">Porta 3 · GigabitEthernet1\/1</);
  ui.run("fillPortSelect([])");
  assert.match(ui.element('config-port').innerHTML, /value="1">Porta 1</);
  assert.equal(ui.element('system-model').textContent, 'Quidway S3026C');
  assert.equal(ui.element('system-writes').textContent, 'Adaptadas · modelo ainda não validado');
  assert.equal(ui.element('ports-note').textContent.startsWith('3 interfaces simuladas'), true);
});
