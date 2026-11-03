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
import sys

from pathlib import Path
from urllib.parse import urlsplit

from faxin_html import is_login_page

USER_DATA_DIR = os.environ.get(
    "FAXIN_USER_DATA_DIR",
    str(Path.home() / ".faxin-mcp" / "browser-data"),
)
FAXIN_BASE = os.environ.get("FAXIN_BASE_URL", "https://www.faxin.cn").rstrip("/")


async def is_logged_in(page, base_url: str = FAXIN_BASE) -> bool | None:
    """验证检索页中的账号标识；异常页面不能证明已登录。"""
    try:
        probe = f"{base_url}/alllibsearch/SearchResult.aspx?usersearchtype=1&keyword=test"
        response = await page.goto(probe, wait_until="domcontentloaded", timeout=30000)
        if response is None or not 200 <= response.status < 300:
            return None
        await page.wait_for_timeout(3000)
        expected, actual = urlsplit(base_url), urlsplit(page.url)
        if (actual.scheme != 'https' or actual.hostname != expected.hostname
                or (actual.port or 443) != (expected.port or 443)
                or actual.username is not None or actual.password is not None):
            return None
        if is_login_page(page.url, ''):
            return False
        for control in await page.locator('form#loginForm, .login-box, input[type="password"]').all():
            if await control.is_visible():
                return False
        for control in await page.locator('a, button, input[type="button"], input[type="submit"]').all():
            if not await control.is_visible():
                continue
            if (await control.get_attribute('type') or '').lower() == 'hidden':
                continue
            label = ((await control.inner_text()) or (await control.get_attribute('value'))
                     or (await control.get_attribute('aria-label')) or (await control.get_attribute('title')) or '')
            if label.strip().lower() in ('退出', '退出登录', '注销', '注销登录', 'logout', 'log out', 'sign out'):
                return True
        return None
    except Exception:
        return None


async def main() -> int:
    from playwright.async_api import async_playwright

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR, headless=False,
                viewport={"width": 1440, "height": 900}, locale="zh-CN",
            )
        except Exception as exc:
            print("❌ 无法启动浏览器，请先停止占用同一用户数据目录的 MCP 服务或浏览器。")
            print(f"   {USER_DATA_DIR}\n   原始错误：{exc}")
            return 1
        try:
            page = browser.pages[0] if browser.pages else await browser.new_page()
            print(f"🌐 正在打开法信官网：{FAXIN_BASE}")
            try:
                await page.goto(FAXIN_BASE, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                print(f"⚠️ 打开网站失败，请在浏览器中手动访问官网。错误：{exc}")
            print("请用本人法信账号在浏览器中完成登录，再回到终端按回车。")
            print("账号密码只经过官网页面；本脚本保存浏览器会话，不读取账号密码。")
            await asyncio.to_thread(input, ">>> 登录完成后按回车：")
            status = await is_logged_in(page)
            cookies = await browser.cookies()
            domains = [c.get('domain', '').lstrip('.') for c in cookies]
            cookie_count = sum(domain == 'faxin.cn' or domain.endswith('.faxin.cn') for domain in domains)
        finally:
            await browser.close()

        if status is not True or cookie_count == 0:
            print("❌ 未能确认登录成功，请检查官网登录状态；页面结构变化也可能导致无法识别。")
            return 1
        print(f"✅ 已确认登录，会话保存在：{USER_DATA_DIR}")
        print("🧪 正在验证无头复用……")
        try:
            reused = await pw.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR, headless=True,
                viewport={"width": 1440, "height": 900}, locale="zh-CN",
            )
            try:
                page = reused.pages[0] if reused.pages else await reused.new_page()
                status = await is_logged_in(page)
            finally:
                await reused.close()
        except Exception as exc:
            print(f"❌ 无头复用验证失败：{exc}")
            return 1
        if status is not True:
            print("❌ 无法确认无头复用仍已登录，请检查会话有效期或登录态识别规则。")
            return 1
        print("✅ 无头复用成功。现在可以重新启动 MCP 服务。")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
