import asyncio
import json
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import server
import login
from mcp.types import CallToolResult
from tests.fakes import BrowserContext, Context, LoginPage, PlaywrightDriver, Response, tiao_html


class ArticleLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_arabic_article_number_keeps_internal_zero(self):
        def respond(method, url, kwargs):
            body = tiao_html('第一百零一条') if kwargs['form']['k'] == '测试法第一百零一条' else ''
            return Response(url, body)

        result = await server.get_tiao(Context(respond), '测试法', '101')
        self.assertEqual(result['tiao'], '第一百零一条')

    async def test_inserted_article_suffix_follows_tiao(self):
        def respond(method, url, kwargs):
            body = tiao_html('第一百条之一') if kwargs['form']['k'] == '测试法第一百条之一' else ''
            return Response(url, body)

        result = await server.get_tiao(Context(respond), '测试法', '第一百条之一')
        self.assertEqual(result['tiao'], '第一百条之一')

    async def test_numbers_with_zeros_are_normalized_consistently(self):
        examples = {'1001': '第一千零一条', '1010': '第一千零一十条',
                    '第一百零一条': '第一百零一条', '1260': '第一千二百六十条'}
        for given, label in examples.items():
            with self.subTest(article=given):
                def respond(method, url, kwargs):
                    body = tiao_html(label) if kwargs['form']['k'] == '测试法' + label else ''
                    return Response(url, body)
                self.assertEqual((await server.get_tiao(Context(respond), '测试法', given))['tiao'], label)


class ArticleExtractionTests(unittest.TestCase):
    def test_cross_reference_does_not_end_an_article(self):
        first = '第一条 当事人依照第二条规定承担责任。'
        self.assertEqual(server.extract_tiao(first + '\n第二条 后续条文。', '1'), first)

    def test_last_article_is_not_silently_cut_at_2000_characters(self):
        article = '第一条 ' + ('示例条文。' * 600)
        self.assertEqual(server.extract_tiao(article, '1'), article)

    def test_inserted_article_ten_does_not_match_eleven(self):
        self.assertIsNone(server.extract_tiao('第一条之十一 这是第十一项增补。', '第一条之十'))

    def test_ambiguous_unspaced_next_heading_is_not_merged(self):
        self.assertIsNone(server.extract_tiao('第一条 为了规范管理。\n第二条本办法适用于下列情形。', '1'))

    def test_ambiguous_unspaced_target_heading_is_not_reported_as_located(self):
        self.assertIsNone(server.extract_tiao('第一条为了规范管理。\n第二条 适用范围。', '1'))


class SearchResultsTests(unittest.TestCase):
    def test_result_keeps_the_group_named_on_the_list_item(self):
        body = '<li class="allmore">司法解释（33） 更多</li><li><a href="/lib/zyfl/zyflcontent.aspx?gid=A123">示例法规</a></li>'
        self.assertEqual(server.parse_first_html(body)[0]['group'], '司法解释（33）')


class DetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_paragraph_start_cross_reference_stays_in_its_article(self):
        body = ('<div class="box fulltext"><p>第一条 当事人应承担责任。</p>'
                '<p>第二条规定的例外情形除外。</p><p>第二条 例外情形。</p></div>')
        result = await server.get_detail(Context(lambda method, url, kwargs: Response(url, body)), 'A123', '1')
        self.assertEqual(result['content'], '第一条 当事人应承担责任。\n第二条规定的例外情形除外。')

    async def test_html_comments_do_not_break_document_parsing(self):
        body = '<!-- header --><title>示例法规</title><div class="box fulltext"><p>第一条<!-- marker --> 正文。</p></div>'
        result = await server.get_detail(Context(lambda method, url, kwargs: Response(url, body)), 'A123', '1')
        self.assertEqual(result['content'], '第一条 正文。')

    async def test_concurrent_details_keep_their_own_document(self):
        def respond(method, url, kwargs):
            title = '法规甲' if 'A123' in url else '法规乙'
            return Response(url, f'<title>{title}</title><div class="box fulltext"><p>第一条 {title}正文。</p></div>')

        ctx = BrowserContext(respond)
        first, second = await asyncio.wait_for(asyncio.gather(
            server.get_detail(ctx, 'A123'), server.get_detail(ctx, 'A456')
        ), timeout=2)
        self.assertEqual((first['title'], second['title']), ('法规甲', '法规乙'))

    async def test_detail_rejects_local_network_urls(self):
        ctx = Context(lambda method, url, kwargs: Response(url, '<title>Private</title>'))
        with self.assertRaises(ValueError):
            await server.get_detail(ctx, 'http://127.0.0.1:8123/private')
        self.assertEqual(ctx.request.visited, [])

    async def test_detail_rejects_redirects_to_another_host(self):
        def respond(method, url, kwargs):
            if kwargs.get('max_redirects') == 0:
                return Response(url, status=302, headers={'location': 'http://127.0.0.1/private'})
            return Response('http://127.0.0.1/private', '<title>Private</title>')

        ctx = Context(respond)
        with self.assertRaises(ValueError):
            await server.get_detail(ctx, 'A123')

    async def test_detail_preserves_paragraphs_metadata_and_cross_references(self):
        article = '第一百零一条 当事人依照第一百零二条规定承担责任。'
        body = ('<title>示例法规 - 法信</title>'
                '<script>document.title="伪造标题";</script>'
                f'<div class="box fulltext"><p>{article}</p><p>第一百零二条 后续条文。</p></div>'
                '<div class="js-sxTab" style="display:none"><p>时效性</p><p>现行有效</p></div>')
        result = await server.get_detail(Context(lambda method, url, kwargs: Response(url, body)), 'A123', '101')
        self.assertEqual(result['content'], article)
        self.assertEqual(result['title'], '示例法规')
        self.assertEqual(result['meta']['时效性'], '现行有效')

    async def test_complete_long_selected_article_is_not_flagged_as_truncated(self):
        article = '第一条 ' + '正文。' * 900
        body = f'<div class="box fulltext"><p>{article}</p></div>'
        result = await server.get_detail(Context(lambda method, url, kwargs: Response(url, body)), 'A123', '1')
        self.assertEqual(result['content'], article)
        self.assertFalse(result['truncated'])

    async def test_http_error_is_not_returned_as_a_document(self):
        for status in (403, 404, 429, 500, 503):
            with self.subTest(status=status):
                ctx = Context(lambda method, url, kwargs: Response(url, '<title>错误页</title>', status=status))
                with self.assertRaisesRegex(ValueError, str(status)):
                    await server.get_detail(ctx, 'A123')

    async def test_same_origin_https_redirect_still_returns_the_document(self):
        def respond(method, url, kwargs):
            if 'gid=A123' in url:
                return Response(url, status=302, headers={'location': '/lib/zyfl/zyflcontent.aspx?gid=A456'})
            return Response(url, '<title>规范地址</title><p>正文。</p>')
        result = await server.get_detail(Context(respond), 'A123')
        self.assertEqual(result['title'], '规范地址')


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_count_response_does_not_prove_login(self):
        def respond(method, url, kwargs):
            if method == 'POST':
                return Response(url, json.dumps({'lib': 'zyfl', 'searchnum': 42}))
            return Response(url, '<form id="loginForm"><input type="password"></form>')

        ok, diagnostic = await server.ensure_login(Context(respond), force=True)
        self.assertFalse(ok, diagnostic)

    async def test_login_script_does_not_accept_a_404_page(self):
        status = await login.is_logged_in(LoginPage('<title>Not found</title>', status=404))
        self.assertIsNone(status)

    async def test_another_sites_account_marker_does_not_prove_faxin_login(self):
        page = LoginPage('<a href="/logout">退出</a>', url='https://sso.example.org/account')
        self.assertIsNone(await login.is_logged_in(page))

    async def test_locked_browser_profile_makes_login_fail(self):
        driver = SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(side_effect=RuntimeError('profile is locked'))))

        class Factory:
            async def start(self):
                return driver

            async def __aenter__(self):
                return driver

            async def __aexit__(self, *args):
                pass

        with patch('playwright.async_api.async_playwright', Factory), redirect_stdout(io.StringIO()):
            result = await login.main()
        self.assertEqual(result, 1)

    async def test_positive_account_marker_is_recognized(self):
        ctx = Context(lambda method, url, kwargs: Response(url, '<a href="/logout.aspx">退出登录</a>'))
        self.assertTrue((await server.ensure_login(ctx, force=True))[0])
        self.assertTrue(await login.is_logged_in(LoginPage('<a href="/logout.aspx">退出登录</a>')))

    async def test_hidden_logout_template_does_not_prove_login(self):
        body = '<div style="display:none"><a href="/logout.aspx">退出</a></div>'
        ctx = Context(lambda method, url, kwargs: Response(url, body))
        self.assertFalse((await server.ensure_login(ctx, force=True))[0])

    async def test_inert_logout_templates_do_not_prove_login(self):
        for tag in ('template', 'noscript'):
            with self.subTest(tag=tag):
                body = f'<{tag}><a href="/logout.aspx">退出</a></{tag}><p>游客</p>'
                ctx = Context(lambda method, url, kwargs: Response(url, body))
                self.assertFalse((await server.ensure_login(ctx, force=True))[0])

    async def test_inert_login_form_does_not_hide_a_real_account_marker(self):
        body = '<template><input type="password"></template><a href="/logout.aspx">退出登录</a>'
        ctx = Context(lambda method, url, kwargs: Response(url, body))
        self.assertTrue((await server.ensure_login(ctx, force=True))[0])

    async def test_hidden_input_cannot_prove_login(self):
        for body in ('<input type="hidden" id="logout" value="0">',
                     '<input type="hidden" value="退出登录">'):
            with self.subTest(body=body):
                ctx = Context(lambda method, url, kwargs: Response(url, body))
                self.assertFalse((await server.ensure_login(ctx, force=True))[0])

    async def test_css_hidden_logout_control_cannot_prove_login(self):
        body = '<style>.logged-in { display:none; }</style><div class="logged-in"><a href="/logout.aspx">退出登录</a></div>'
        ctx = Context(lambda method, url, kwargs: Response(url, body))
        self.assertFalse((await server.ensure_login(ctx, force=True))[0])

    async def test_forced_failed_login_check_clears_a_previous_success(self):
        logged_in = True
        def respond(method, url, kwargs):
            body = '<a href="/logout.aspx">退出</a>' if logged_in else '<form id="loginForm"></form>'
            return Response(url, body)
        ctx = Context(respond)
        self.assertTrue((await server.ensure_login(ctx, force=True))[0])
        logged_in = False
        self.assertFalse((await server.ensure_login(ctx, force=True))[0])
        self.assertFalse((await server.ensure_login(ctx))[0])


class OutlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_outline_parses_counts_and_law_excerpts_offline(self):
        body = ('<div class="right_navation"><label>A2.E6买卖合同纠纷</label><p>合同 &gt; 买卖合同</p></div>'
                '<li class="navation_item">法 律 12</li>'
                '<div class="content_item"><a href="/lib/zyfl/zyflcontent.aspx?gid=A123">示例法规</a>'
                '<p>第一条 摘录。</p></div>')
        result = await server.get_outline(Context(lambda method, url, kwargs: Response(url, body)), '123')
        self.assertEqual((result['name'], result['code']), ('买卖合同纠纷', 'A2.E6'))
        self.assertEqual(result['counts'], {'法律': '12'})
        self.assertEqual(result['laws'][0]['excerpt'], '第一条 摘录。')


class CaseSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_site_unsupported_feature_is_not_session_expiry(self):
        ctx = Context(lambda method, url, kwargs: Response(url, '<html>首页</html>'))
        with self.assertRaisesRegex(ValueError, '主站.*不支持'):
            await server.search_lalei(ctx, keyword='合同')

    async def test_case_api_access_denial_is_not_an_empty_search(self):
        def respond(method, url, kwargs):
            if method == 'GET':
                return Response(url, '<a href="https://wenshu.faxin.cn/wenshu/page/laIndex.html?token=fixture-token">类案</a>')
            return Response(url, json.dumps({'status': 403}), status=403)
        with patch.object(server, 'FAXIN_BASE', 'https://sfb-vip.faxin.cn'):
            with self.assertRaisesRegex(ValueError, '403'):
                await server.search_lalei(Context(respond), keyword='合同')

    async def test_permission_denied_searches_are_mcp_errors(self):
        def respond(method, url, kwargs):
            if method == 'GET':
                return Response(url, '<a href="/logout.aspx">退出登录</a>')
            if 'GetLibSearchResultNum' in url:
                return Response(url, json.dumps({'searchnum': 1}))
            return Response(url, json.dumps({'AllCount': 1, 'FirstHtml': '<div class="no_content"></div>'}))
        for name, arguments in [('faxin_search_case', {'keyword': '合同'}),
                                ('faxin_search_lib', {'keyword': '合同', 'lib': 'flqk'})]:
            with self.subTest(tool=name):
                driver = PlaywrightDriver(Context(respond))
                with patch('playwright.async_api.async_playwright', lambda: driver):
                    try:
                        result = await server.call_tool(name, arguments)
                    finally:
                        await server.close_context()
                self.assertIsInstance(result, CallToolResult)
                self.assertTrue(result.isError)


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_releases_the_profile_and_driver(self):
        ctx = Context(lambda method, url, kwargs: Response(url))
        driver = PlaywrightDriver(ctx)
        with patch('playwright.async_api.async_playwright', lambda: driver):
            await server.get_context()
            await server.close_context()
        self.assertTrue(ctx.closed)
        self.assertTrue(driver.stopped)

    async def test_failed_launch_stops_the_driver(self):
        ctx = Context(lambda method, url, kwargs: Response(url))
        driver = PlaywrightDriver(ctx)
        driver.launch_persistent_context = AsyncMock(side_effect=RuntimeError('profile is locked'))
        with patch('playwright.async_api.async_playwright', lambda: driver):
            with self.assertRaisesRegex(RuntimeError, 'profile is locked'):
                await server.get_context()
        self.assertTrue(driver.stopped)
