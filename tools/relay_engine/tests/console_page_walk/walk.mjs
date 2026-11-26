// The test fixture is deliberately separate from the production page and engine.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';

const id = '11111111-1111-7111-8111-111111111111';
const missingId = '44444444-4444-7444-8444-444444444444';
const title = 'Which branch first?';
const refreshedTitle = 'Which branch after recovery?';
const signedOut = 'This page is not signed in. To open it, run adt console open in a terminal.';
const lost = 'Cannot reach the engine, so this page may be out of date. Run adt engine status.';
const ready = page => page.getByRole('heading', { name: 'Waiting for you · 1', exact: true }).waitFor();
const heading = (page, name) => page.getByRole('heading', { name, exact: true, level: 1 });
const status = (page, text) => page.getByRole('status').filter({ hasText: text });
const copy = line => line.getByRole('button', { name: 'Copy command', exact: true });
const send = page => page.getByRole('button', { name: 'Send answer', exact: true });
const open = async page => {
  await page.getByRole('button', { name: 'Answer: ' + title, exact: true }).click();
  await heading(page, title).waitFor();
};
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const sessions = requests => requests.filter(request => new URL(request.url()).pathname === '/api/session');
const acts = requests => requests.filter(request => new URL(request.url()).pathname.startsWith('/api/op/'));

const walks = new Map(Object.entries({
  async smoke(page) {
    await page.goto(origin);
    assert.equal(await page.locator('#smoke').count(), 1);
  },
  async sign_in(page, requests) {
    const script = page.waitForResponse(response => response.url() === origin + '/page.js');
    const launch = launchUrl();
    await page.goto(launch);
    const received = sha256(await (await script).body());
    const onDisk = sha256(readFileSync(resolve(pageDir, 'page.js')));
    assert.equal(received, onDisk, 'page.js response must match the selected page directory');
    console.log('page.js sha256 ' + received);
    await ready(page);
    assert.ok(!new URL(page.url()).hash, 'launch fragment remained visible');
    const [session] = sessions(requests);
    assert.equal(sessions(requests).length, 1, 'expected one session exchange');
    assert.equal(session.method(), 'POST', 'launch did not POST a session');
    const secret = new URLSearchParams(new URL(launch).hash.slice(1)).get('launch');
    assert.equal(session.postDataJSON()?.launch === secret, true, 'incorrect launch payload');
  },
  async sign_in_item(page) {
    await page.goto(launchUrl() + '&item=' + id);
    await heading(page, title).waitFor();
    assert.ok(!new URL(page.url()).hash, 'deep link fragment remained visible');
    const next = await restart();
    await page.goto('about:blank');
    await page.goto(next + '&item=' + missingId);
    await ready(page);
    assert.equal(await page.getByText('That question is no longer available.', { exact: true }).count(), 1);
  },
  async two_tabs(page) {
    await page.goto(launchUrl());
    await ready(page);
    const second = await page.context().newPage();
    try {
      const request = second.waitForRequest(candidate => candidate.url() === origin + '/api/session');
      await second.goto(origin);
      assert.equal((await request).method(), 'GET', 'second tab did not get the existing session');
      await ready(second);
    } finally {
      await second.close();
    }
  },
  async restart_drops_session(page, requests) {
    await page.goto(launchUrl());
    await open(page);
    const before = await evidence();
    assert.equal(before.seq, '1');
    assert.ok(before.lookups.includes('queue.get'));
    assert.deepEqual(before.requests, []);
    assert.notEqual(await restart(), initialLaunch, 'fixture did not rotate the launch URL');
    const refused = page.waitForResponse(response => response.url() === origin + '/api/op/queue.answer');
    await page.getByRole('button', { name: 'release first', exact: true }).click();
    assert.equal((await refused).status(), 403, 'the pre-restart act was not refused');
    const notice = status(page, signedOut);
    await notice.waitFor();
    assert.equal(await copy(notice).count(), 1);
    assert.equal(await send(page).count(), 0);
    const after = await evidence();
    assert.equal(after.seq, '2');
    assert.deepEqual(after.requests, before.requests, 'refused session reached the authenticated append point');
    assert.ok(!after.lookups.includes('queue.answer'), 'refused session reached an answer lookup');
    assert.deepEqual(sessions(requests).map(request => request.method()), ['POST', 'GET']);
    assert.equal(acts(requests).length, 1, 'the page retried a refused act');
    const first = acts(requests)[0];
    const oldToken = await first.headerValue('x-adt-csrf');
    assert.ok(oldToken, 'the first act did not carry its original token');
    for (const later of requests.slice(requests.indexOf(first) + 1)) {
      assert.equal(await later.headerValue('x-adt-csrf') === oldToken, false, 'a later request reused the old token');
    }
    console.log('fixture launch rotated; evidence seq ' + before.seq + ', ' + after.seq);
  },
  async not_signed_in(page, requests) {
    const refused = page.waitForResponse(response => response.url() === origin + '/api/session');
    await page.goto(origin + '/#launch=refused-fixture-launch');
    assert.equal((await refused).status(), 403, 'the invalid launch was not refused');
    const notice = status(page, signedOut);
    await notice.waitFor();
    assert.ok(!new URL(page.url()).hash);
    assert.equal(await copy(notice).count(), 1);
    assert.equal(acts(requests).length, 0);
  },
  async lost_engine(page, requests) {
    await page.goto(launchUrl());
    await open(page);
    const abort = route => route.abort();
    await page.route('**/api/read/**', abort);
    const notice = status(page, lost);
    try {
      await notice.waitFor();
      assert.equal(await copy(notice).count(), 1);
      assert.equal(await send(page).isDisabled(), true, 'the answer button stayed enabled while reads failed');
      const choice = page.getByRole('button', { name: 'release first', exact: true });
      assert.equal(await choice.isDisabled(), true, 'a choice stayed enabled while reads failed');
      await choice.evaluate(button => button.click());
      assert.equal(acts(requests).length, 0, 'a control sent an act while reads failed');
    } finally {
      await page.unroute('**/api/read/**', abort);
    }
    await heading(page, refreshedTitle).waitFor();
    assert.equal(await notice.count(), 0);
    assert.equal(await send(page).isEnabled(), true, 'the answer button stayed disabled after refresh');
  },
  async dark_scheme(page) {
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto(launchUrl());
    await ready(page);
    const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    const light = await background();
    await page.emulateMedia({ colorScheme: 'dark' });
    assert.notEqual(await background(), light, 'the background did not respond to the system theme');
    assert.equal(await page.getByRole('switch').count(), 0);
  },
}));

const inputs = process.argv.slice(2);
if (inputs[0] === '--validate') {
  for (const name of inputs.slice(1)) {
    if (!walks.has(name) || (name !== 'smoke' && !existsSync(new URL('./scenarios/' + name + '.json', import.meta.url)))) {
      console.error('console page walk: unknown scenario: ' + name);
      process.exit(2);
    }
  }
  process.exit(0);
}

const [originFile, launchFile, evidenceFile, fixturePid, pageDir, name] = inputs;
if (!walks.has(name)) {
  console.error('console page walk: unknown scenario: ' + name);
  process.exit(2);
}
const toolchain = process.env.ADT_PAGE_TOOLCHAIN;
if (!toolchain || process.env.PLAYWRIGHT_BROWSERS_PATH !== toolchain + '/browsers')
  throw new Error('console page walk: browser path must be the toolchain browsers folder');
const require = createRequire(resolve(toolchain, 'package.json'));
const { chromium } = require('playwright');

const origin = readFileSync(originFile, 'utf8').trim();
const address = new URL(origin);
assert.equal(address.protocol, 'http:');
assert.equal(address.hostname, '127.0.0.1');
assert.equal(address.origin, origin);
assert.match(fixturePid, /^[1-9][0-9]*$/);

const launchUrl = () => readFileSync(launchFile, 'utf8').trim();
const initialLaunch = launchUrl();
let lastSeq = 0n;
async function waitForFixture(read, failure) {
  const deadline = performance.now() + 5000;
  while (performance.now() < deadline) {
    const result = read();
    if (result !== undefined) return result;
    await delay(Math.min(25, Math.max(0, deadline - performance.now())));
  }
  throw new Error(failure);
}
async function restart() {
  const previous = launchUrl();
  process.kill(Number(fixturePid), 'SIGUSR1');
  return waitForFixture(() => {
    const current = launchUrl();
    if (current !== previous && current.startsWith(origin + '/#launch=')) return current;
  }, 'fixture did not issue a fresh launch URL within 5 s');
}
async function evidence() {
  process.kill(Number(fixturePid), 'SIGUSR2');
  const value = await waitForFixture(() => {
    if (!existsSync(evidenceFile)) return;
    const snapshot = JSON.parse(readFileSync(evidenceFile, 'utf8'));
    assert.match(snapshot.seq, /^[1-9][0-9]*$/);
    if (BigInt(snapshot.seq) > lastSeq) return snapshot;
  }, 'fixture did not publish a newer evidence snapshot within 5 s');
  lastSeq = BigInt(value.seq);
  assert.ok(Array.isArray(value.requests));
  assert.ok(Array.isArray(value.lookups));
  return value;
}

await test(name, async () => {
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (error) {
    console.error('console page walk: browser launch refused: ' + error.message);
    throw error;
  }
  try {
    const context = await browser.newContext({ timezoneId: 'UTC', serviceWorkers: 'block' });
    try {
      await context.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.origin !== origin || url.hostname !== '127.0.0.1')
          return route.abort('blockedbyclient');
        await route.continue();
      });
      const page = await context.newPage();
      const requests = [];
      context.on('request', request => requests.push(request));
      await walks.get(name)(page, requests);
    } finally {
      await context.close();
    }
  } finally {
    await browser.close();
  }
});
