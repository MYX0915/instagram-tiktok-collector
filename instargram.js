const { chromium } = require('playwright');
const { mkdir, writeFile, chmod } = require('node:fs/promises');
const path = require('node:path');
const readline = require('node:readline/promises');

async function main() {
    const browser = await chromium.launch({
        channel: 'chrome',
        headless: false
    });
    const terminal = readline.createInterface({
        input: process.stdin,
        output: process.stdout
    });

    try {
        const context = await browser.newContext();
        const page = await context.newPage();
        await page.goto('https://www.instagram.com/', {
            waitUntil: 'domcontentloaded',
            timeout: 60000
        });
        await terminal.question('请在 Chrome 中完成 Instagram 登录，然后回到此终端按回车保存 Cookie：');

        const cookies = (await context.cookies()).filter((cookie) => {
            const domain = cookie.domain.replace(/^\./, '');
            return domain === 'instagram.com' || domain.endsWith('.instagram.com');
        });
        if (!cookies.some((cookie) => cookie.name === 'sessionid' && cookie.value)) {
            throw new Error('未找到 Instagram sessionid，请确认已登录后重新运行；未写入账号文件。');
        }

        const accountDir = path.join(__dirname, 'account');
        const cookiePath = path.join(accountDir, 'instargram.cookies.json');
        await mkdir(accountDir, { recursive: true, mode: 0o700 });
        await writeFile(cookiePath, JSON.stringify(cookies, null, 4), { mode: 0o600 });
        await chmod(cookiePath, 0o600);
        console.log(`Cookie 已保存至：${cookiePath}`);
    } finally {
        terminal.close();
        await browser.close();
    }
}

main().catch((error) => {
    console.error(`保存失败：${error.message}`);
    process.exitCode = 1;
});
