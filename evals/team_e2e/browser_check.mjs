// 浏览器点验：搜索 → around_id 锚点窗口 → 高亮与停位 → 回到最新 → 成员变更刷新。
// 环境由 run.mjs 注入：TEAM_E2E_WEB、TEAM_E2E_API、TEAM_E2E_WORK、TEAM_E2E_CHROME、TEAM_E2E_PLAYWRIGHT。
// 缺 Chrome 或 playwright-core 时由 run.mjs 判定并显式 SKIP，不会跑到这里。
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';

const WEB = process.env.TEAM_E2E_WEB;
const API = process.env.TEAM_E2E_API;
const WORK = process.env.TEAM_E2E_WORK;
const { chromium } = await import(pathToFileURL(process.env.TEAM_E2E_PLAYWRIGHT).href);

const seed = JSON.parse(fs.readFileSync(`${WORK}/seed.json`, 'utf8'));
const OLD_ID = seed.backend_message_id;
const GROUP = seed.group_id;
const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' :: ' + detail : ''}`);
}

// 上一次跑过的成员改动要还原，否则 C1 的成员数断言不是装置问题就是陈旧状态。
const reset = await fetch(`${API}/api/v1/team/groups/${GROUP}`, {
  method: 'PATCH',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ member_ids: [seed.backend_bot_id] }),
});
if (!reset.ok) {
  console.log('reset members failed', reset.status, await reset.text());
  process.exit(2);
}

const browser = await chromium.launch({ executablePath: process.env.TEAM_E2E_CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const consoleErrors = [];
const requests = [];
page.on('console', (msg) => {
  if (msg.type() === 'error') consoleErrors.push(msg.text());
});
// 点验失败时最先要看的就是「页面到底发了什么请求」：锚点跳转是 fetch + 状态切换，
// 光看 DOM 分不清是没请求、请求错了还是被后续轮询覆盖。
page.on('request', (request) => {
  const url = request.url();
  if (url.includes('/api/v1/')) requests.push(`${request.method()} ${url.replace(/^https?:\/\/[^/]+/, '')}`);
});

async function setInput(selector, value) {
  await page.evaluate(
    ([sel, val]) => {
      const el = document.querySelector(sel);
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(el, val);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    },
    [selector, value],
  );
}
const clickText = (text) =>
  page.evaluate((needle) => {
    const node = [...document.querySelectorAll('button')].find((b) => b.innerText.includes(needle));
    if (!node) return false;
    node.click();
    return true;
  }, text);
const hasText = (text) => page.evaluate((needle) => document.body.innerText.includes(needle), text);

try {
  await page.goto(`${WEB}/team`, { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForSelector('[aria-label="搜索聊天与结论"]', { timeout: 30000 });
  await page.waitForTimeout(2500);
  // 没自动选中群时点一下侧栏；已选中则 selectGroup 自己短路，无副作用
  await clickText('交付群');
  await page.waitForTimeout(2500);

  // C1 侧栏出现种下的群，成员数是真实值
  const sideText = await page.evaluate(() => document.body.innerText);
  check('C1 侧栏出现「交付群」且成员数为 2', sideText.includes('交付群') && /交付群[\s\S]{0,40}2/.test(sideText), '');

  // C2 装置自检：默认窗口确实装满 120 条，且不含要搜的那条旧结论
  const absentBefore = await page.evaluate(
    (id) => ({
      loaded: document.querySelectorAll('[data-message-id]').length,
      absent: !document.getElementById(`team-message-${id}`),
    }),
    OLD_ID,
  );
  check(
    'C2 已加载 120 条窗口且旧结论不在其中（装置自检）',
    absentBefore.absent && absentBefore.loaded === 120,
    JSON.stringify(absentBefore),
  );

  // C3 搜索跨群命中，分词高亮（「种子结论」只有铺数据那条结论有，后续轮次的重复结论不会抢目标）
  await setInput('[aria-label="搜索聊天与结论"]', '种子结论');
  await page.waitForFunction(
    () => [...document.querySelectorAll('button')].some((b) => b.innerText.includes('后端') && b.innerText.includes('种子结论')),
    null,
    { timeout: 15000 },
  );
  const hitBox = await page.evaluate(() => {
    const b = [...document.querySelectorAll('button')].find((x) => x.innerText.includes('种子结论'));
    if (!b) return null;
    return { text: b.innerText.replace(/\s+/g, ' '), marks: b.querySelectorAll('mark').length };
  });
  check('C3 搜索结果命中旧结论并高亮分词', !!hitBox && hitBox.marks > 0, JSON.stringify(hitBox));
  await page.screenshot({ path: `${WORK}/shot-search.png` });

  // C4 点结果后加载锚点窗口、居中并高亮
  const clicked = await clickText('种子结论');
  await page.waitForSelector(`#team-message-${OLD_ID}`, { timeout: 20000 });
  const jumped = await page.evaluate(
    (id) => {
      const el = document.getElementById(`team-message-${id}`);
      const rect = el.getBoundingClientRect();
      return {
        banner: document.body.innerText.includes('正在查看搜索结果附近的消息'),
        text: el.innerText.replace(/\s+/g, ' ').slice(0, 120),
        ring: el.className.includes('ring-brand'),
        centerY: rect.top + rect.height / 2,
        inViewport: rect.top >= 0 && rect.bottom <= window.innerHeight,
        count: document.querySelectorAll('[data-message-id]').length,
      };
    },
    OLD_ID,
  );
  check('C4 点结果后加载到旧消息附近窗口并显示提示条', clicked && jumped.banner, JSON.stringify(jumped));
  // 平滑滚动是动画：中途取样只会得到「正在穿过」。等两次取样一致再判落点。
  await page
    .waitForFunction(
      (id) => {
        const el = document.getElementById(`team-message-${id}`);
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.top >= 0 && r.bottom <= window.innerHeight;
      },
      OLD_ID,
      { timeout: 6000 },
    )
    .catch(() => {});
  const rectNow = (id) =>
    page.evaluate((messageId) => {
      const el = document.getElementById(`team-message-${messageId}`);
      const r = el.getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom), vh: window.innerHeight };
    }, id);
  await page.waitForTimeout(1500);
  const settle = await rectNow(OLD_ID);
  await page.waitForTimeout(1500);
  const after = await rectNow(OLD_ID);
  const inView = (b) => b.top >= 0 && b.bottom <= b.vh;
  const offCenter = Math.abs((settle.top + settle.bottom) / 2 - settle.vh / 2);
  check('C4b 目标消息落在视口内且带高亮', inView(settle) && jumped.ring, JSON.stringify({ settle, ring: jumped.ring }));
  check(
    'C4b2 动画结束后停住不漂（居中、不被自动滚底抢走）',
    inView(after) && Math.abs(after.top - settle.top) < 24 && offCenter < settle.vh * 0.2,
    JSON.stringify({ settle, after, offCenter: Math.round(offCenter) }),
  );
  check('C4c 锚点窗口消息数远小于全量（63 条左右）', jumped.count > 0 && jumped.count <= 121, `count=${jumped.count}`);
  await page.screenshot({ path: `${WORK}/shot-jump.png` });

  // C5 回到最新清掉锚点窗口
  await clickText('回到最新');
  await page.waitForTimeout(2500);
  const backLatest = await page.evaluate(
    (id) => ({
      gone: !document.getElementById(`team-message-${id}`),
      banner: document.body.innerText.includes('正在查看搜索结果附近的消息'),
      count: document.querySelectorAll('[data-message-id]').length,
    }),
    OLD_ID,
  );
  check('C5 回到最新后旧锚点窗口消失', backLatest.gone && !backLatest.banner && backLatest.count === 120, JSON.stringify(backLatest));

  // C6 用页面自己走的 HTTP 改成员，侧栏与成员条要在轮询后刷新
  const patched = await page.evaluate(
    async ([gid, ids]) => {
      const resp = await fetch(`/api/v1/team/groups/${gid}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ member_ids: ids }),
      });
      return { status: resp.status, ok: resp.ok };
    },
    [GROUP, [seed.backend_bot_id, seed.frontend_bot_id]],
  );
  check('C6a 页面上下文 PATCH 成员成功', patched.ok, JSON.stringify(patched));
  await page.waitForFunction(
    () => {
      const strip = [...document.querySelectorAll('*')].some((n) => n.childElementCount === 0 && n.textContent.trim() === '前端');
      return strip && /交付群[\s\S]{0,40}3/.test(document.body.innerText);
    },
    null,
    { timeout: 30000 },
  );
  const refreshed = await page.evaluate(() => {
    const text = document.body.innerText;
    return { sidebarThree: /交付群[\s\S]{0,40}3/.test(text), stripHasFrontend: text.includes('前端') };
  });
  check('C6b 轮询后侧栏成员数变 3、成员条出现「前端」', refreshed.sidebarThree && refreshed.stripHasFrontend, JSON.stringify(refreshed));
  await page.screenshot({ path: `${WORK}/shot-members.png` });

  // C7 回填过的历史也能搜到
  await setInput('[aria-label="搜索聊天与结论"]', '填充消息');
  await page.waitForFunction(
    () => [...document.querySelectorAll('button')].filter((b) => b.innerText.includes('填充消息')).length >= 5,
    null,
    { timeout: 15000 },
  );
  const fillerHits = await page.evaluate(
    () => [...document.querySelectorAll('button')].filter((b) => b.innerText.includes('填充消息')).length,
  );
  check('C7 历史消息已建索引，可批量搜到', fillerHits >= 5, `hits=${fillerHits}`);

  // C8 无命中时给空态而不是报错。用纯无意义拉丁词：中文短语会被 unigram 召回常用字，
  // 「绝不可能存在的词」本身是能搜出结果的，拿它判空态等于判了个假前提。
  await setInput('[aria-label="搜索聊天与结论"]', 'zzqqxxnn');
  await page.waitForFunction(() => document.body.innerText.includes('没有找到'), null, { timeout: 15000 });
  check('C8 无命中时给出空态而不是报错', await hasText('没有找到'));
} catch (error) {
  check('脚本执行', false, String(error).slice(0, 500));
  await page.screenshot({ path: `${WORK}/shot-error.png` }).catch(() => {});
} finally {
  check('C9 无控制台错误', consoleErrors.filter((t) => !/favicon|ResizeObserver/.test(t)).length === 0, consoleErrors.slice(0, 3).join(' | '));
  const anchorRequests = requests.filter((line) => line.includes('around_id'));
  check('C0 点结果确实发出了带 around_id 的锚点请求', anchorRequests.length > 0, anchorRequests.slice(-3).join(' | ') || '无 around_id 请求');
  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} 通过`);
  if (failed.length) console.log(failed.map((f) => f.name).join(', '));
  fs.writeFileSync(
    `${WORK}/browser-checks.json`,
    JSON.stringify({ viewport: '1440x900', headless: true, results, requests: requests.slice(-60) }, null, 2),
    'utf8',
  );
  process.exit(failed.length ? 1 : 0);
}
