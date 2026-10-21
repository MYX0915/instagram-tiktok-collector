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
        await page.goto('https://www.tiktok.com/login', {
            waitUntil: 'domcontentloaded',
            timeout: 60000
        });
        await terminal.question('请在 Chrome 中手动完成 TikTok 登录，确认登录成功后回到此终端按回车保存 Cookie：');

        const cookies = (await context.cookies()).filter((cookie) => {
            const domain = cookie.domain.replace(/^\./, '');
            return domain === 'tiktok.com' || domain.endsWith('.tiktok.com');
        });
        if (cookies.length === 0) {
            throw new Error('没有取得 TikTok Cookie，未写入账号文件。');
        }

        const accountDir = path.join(__dirname, 'account');
        const cookiePath = path.join(accountDir, 'tiktok.cookies.json');
        await mkdir(accountDir, { recursive: true, mode: 0o700 });
        await writeFile(cookiePath, JSON.stringify(cookies, null, 4), { mode: 0o600 });
        await chmod(cookiePath, 0o600);
        console.log(`Cookie 已保存至：${cookiePath}`);
        console.log('此步骤仅保存 Cookie，登录是否有效由你在网页中确认。');
    } finally {
        terminal.close();
        await browser.close();
    }
}

main().catch((error) => {
    console.error(`保存失败：${error.message}`);
    process.exitCode = 1;
});
