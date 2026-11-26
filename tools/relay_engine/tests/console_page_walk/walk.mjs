// The test fixture is deliberately separate from the production page and engine.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const toolchain = process.env.ADT_PAGE_TOOLCHAIN;
if (!toolchain || process.env.PLAYWRIGHT_BROWSERS_PATH !== `${toolchain}/browsers`)
  throw new Error('console page walk: browser path must be the toolchain browsers folder');
const require = createRequire(resolve(toolchain, 'package.json'));
const { chromium } = require('playwright');
const [originFile, ...requested] = process.argv.slice(2);
const origin = readFileSync(originFile, 'utf8').trim();
const address = new URL(origin);
assert.equal(address.protocol, 'http:');
assert.equal(address.hostname, '127.0.0.1');
assert.equal(address.origin, origin);
const scenarios = requested.length ? requested : ['smoke'];
// Only Task 1's smoke exists here. Later tasks add their own named walks.
for (const name of scenarios)
  assert.equal(name, 'smoke', `unknown console page scenario: ${name}`);
let browser;
try {
  browser = await chromium.launch({ headless: true });
} catch (error) {
  console.error(`console page walk: browser launch refused: ${error.message}`);
  throw error; // No retry or system-setting workaround.
}
let passed = 0;
let failed = 0;
try {
  for (const name of scenarios) {
    await test(name, async () => {
      const context = await browser.newContext({ timezoneId: 'UTC', serviceWorkers: 'block' });
      try {
        await context.route('**/*', async route => {
          const url = new URL(route.request().url());
          if (url.origin !== origin || url.hostname !== '127.0.0.1')
            return route.abort('blockedbyclient');
          await route.continue();
        });
        const page = await context.newPage();
        await page.goto(origin);
        assert.equal(await page.locator('#smoke').count(), 1);
        passed++;
      } catch (error) {
        failed++;
        throw error;
      } finally {
        await context.close();
      }
    });
  }
} finally {
  await browser.close();
}
console.log(`console page walk: ${passed} passed, ${failed} failed`);
