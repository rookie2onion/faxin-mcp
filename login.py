#!/usr/bin/env python3
"""
法信 (faxin.cn) 登录脚本 —— 独立运行。

本脚本不做任何自动登录：它只是打开一个带持久化用户数据目录的浏览器窗口，
由使用者本人在法信官网页面上完成登录（账号密码 / 短信验证码 / 扫码，
以官网提供的登录方式为准）。登录成功后按回车，会话 cookie 即持久化到
本地目录，供 faxin MCP server 复用。

用法（终端直接执行）：
  python login.py

环境变量：
  FAXIN_BASE_URL       法信入口，默认 https://www.faxin.cn
  FAXIN_USER_DATA_DIR  浏览器用户数据目录，默认 ~/.faxin-mcp/browser-data

注意：
  - 登录期间请勿同时运行 MCP server（用户数据目录锁冲突）；
  - 本脚本不会读取、记录或上传你的账号密码——所有凭据只经过法信官网页面。
"""
import asyncio
import os
from pathlib import Path

USER_DATA_DIR = os.environ.get(
    "FAXIN_USER_DATA_DIR",
    str(Path.home() / ".faxin-mcp" / "browser-data"),
)
FAXIN_BASE = os.environ.get("FAXIN_BASE_URL", "https://www.faxin.cn")


async def is_logged_in(page) -> bool | None:
    """判断法信当前是否处于登录态。返回 True/False/None(不确定)。

    判定策略：直接访问需要登录的检索页而非首页——首页对游客也显示
    静态导航，不能作为登录凭证。未登录时检索页会被重定向到登录页
    或直接呈现登录表单。
    """
    try:
        probe = f"{FAXIN_BASE}/alllibsearch/SearchResult.aspx?usersearchtype=1&keyword=test"
        await page.goto(probe, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        url = page.url.lower()
        if "login" in url:
            return False
        login_form = await page.evaluate("""() => {
            const txt = document.body ? document.body.innerText : '';
            return Boolean(document.querySelector('form#loginForm, .login-box, input[name=username]'))
                   || txt.includes('请输入账号') || txt.includes('请先登录') || txt.includes('登录法信');
        }""")
        if login_form:
            return False
        return True
    except Exception:
        return None


async def main():
    from playwright.async_api import async_playwright

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,  # 有头：弹出可见浏览器窗口供手动登录
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            # 注意：本项目刻意不使用任何隐藏自动化特征 / 规避检测的启动参数。
        )
    except Exception as e:
        print("❌ 无法启动浏览器。常见原因：另一个浏览器实例或 MCP server 正在占用")
        print(f"   {USER_DATA_DIR} 的锁。请关闭后重试。")
        print(f"   原始错误：{e}")
        return

    page = browser.pages[0] if browser.pages else await browser.new_page()

    print("🌐 正在打开法信官网 ...")
    print(f"   {FAXIN_BASE}")
    try:
        await page.goto(FAXIN_BASE, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"⚠️ 打开法信失败（{e}），请手动在浏览器里输入 {FAXIN_BASE}。")

    print()
    print("=" * 60)
    print("【请在该浏览器窗口中，手动完成以下操作】")
    print("  1. 在法信官网点击「登录」，用你本人的法信账号完成登录")
    print("     （账号密码 / 短信验证码 / 扫码，以官网提供的登录方式为准）")
    print("  2. 确认页面右上角已显示已登录状态")
    print("  3. 回到本终端按【回车】保存登录态")
    print()
    print("  本脚本不会读取、记录或上传你的账号密码——")
    print("  所有凭据只经过法信官网页面。")
    print("=" * 60)
    input(">>> 完成上述操作后按回车继续：")

    # 跳到检索页验证登录态（不是首页——首页有静态导航会误判）
    print("🔍 正在验证登录态 ...")
    try:
        probe = f"{FAXIN_BASE}/alllibsearch/SearchResult.aspx?usersearchtype=1&keyword=test"
        await page.goto(probe, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"⚠️ 打开检索页失败（{e}）。")
    await page.wait_for_timeout(3000)

    status = await is_logged_in(page)

    faxin_cookies = await browser.cookies()
    faxin_cookie_count = len([c for c in faxin_cookies if 'faxin' in c.get('domain', '')])

    if status is True and faxin_cookie_count > 0:
        print(f"✅ 已确认法信处于登录态！（法信域 cookie: {faxin_cookie_count} 条）")
        print("   会话已持久化到：")
        print(f"   {USER_DATA_DIR}")
    elif status is True and faxin_cookie_count == 0:
        print("⚠️ 检索页可访问但法信域无 cookie，会话可能不稳定。")
        print("   建议回到浏览器确认已登录后重跑本脚本。")
    elif status is False:
        print("⚠️ 法信仍显示未登录。请确认已在浏览器中完成登录后重跑本脚本。")
    else:
        print("❓ 无法自动判断登录态，请自行确认法信页面是否已登录。")
        print(f"   会话数据已尝试保存到：{USER_DATA_DIR}")

    await browser.close()

    # 无头复用验证：确认 MCP server（headless）能直接复用该会话
    print()
    print("🧪 正在用无头模式复开法信，验证 headless 复用是否保持登录 ...")
    try:
        pw2 = await async_playwright().start()
        b2 = await pw2.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=True,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        p2 = b2.pages[0] if b2.pages else await b2.new_page()
        await p2.goto(FAXIN_BASE + "/index.aspx", wait_until="domcontentloaded", timeout=30000)
        await p2.wait_for_timeout(2500)
        reuse = await is_logged_in(p2)
        await b2.close()
        if reuse is True:
            print("✅ 无头复用成功：MCP server 可直接使用，无需每次重登。")
        elif reuse is False:
            print("⚠️ 无头复用失败：会话可能依赖动态 token，无法仅靠持久化 cookie 维持。")
            print("   可重跑本脚本再试；若持续失败请提 issue。")
        else:
            print("❓ 无头复用状态无法确定，建议实际检索一次验证。")
    except Exception as e:
        print(f"⚠️ 无头复用验证出错（{e}），不影响已保存的会话，可稍后手动验证。")

    print()
    print("下一步：在你的 MCP 客户端中配置 server.py（见 README.md），即可开始检索。")


if __name__ == "__main__":
    asyncio.run(main())
