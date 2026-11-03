"""Offline fixtures for the external Playwright request boundary."""
import asyncio
import re
from lxml import html


class Response:
    def __init__(self, url, body='', status=200, headers=None):
        self.url = url
        self.body = body
        self.status = status
        self.headers = headers or {}

    async def text(self):
        return self.body

    async def dispose(self):
        pass


class Request:
    def __init__(self, respond):
        self.respond = respond
        self.visited = []

    async def _send(self, method, url, kwargs):
        self.visited.append((method, url))
        await asyncio.sleep(0)
        return self.respond(method, url, kwargs)

    async def get(self, url, **kwargs):
        return await self._send('GET', url, kwargs)

    async def post(self, url, **kwargs):
        return await self._send('POST', url, kwargs)


class Context:
    def __init__(self, respond):
        self.request = Request(respond)
        self.closed = False

    async def cookies(self):
        if self.closed:
            raise RuntimeError('Browser context closed')
        return []

    async def close(self):
        self.closed = True

    async def new_page(self):
        return LoginPage('', request=self.request)


class PlaywrightDriver:
    def __init__(self, context):
        self.context = context
        self.chromium = self
        self.stopped = False

    async def start(self):
        return self

    async def launch_persistent_context(self, **kwargs):
        return self.context

    async def stop(self):
        self.stopped = True


def tiao_html(label, text='示例条文。'):
    return (
        '<a href="/lib/Zyfl/ZyflContent.aspx?gid=A123#101">测试法</a>'
        '<br/><span>2026.01.01施行</span>'
        f'<div class="zyfl_tiao_tiaoinfo">{label} {text}</div>'
    )


class BrowserContext(Context):
    """Models a browser page whose document can be replaced during an await."""
    def __init__(self, respond):
        super().__init__(respond)
        self.page = BrowserPage()

    async def new_page(self):
        return self.page


class BrowserPage:
    def __init__(self):
        self.body = ''
        self.second_document = asyncio.Event()

    def is_closed(self):
        return False

    async def set_content(self, body, **kwargs):
        self.body = body
        if '法规甲' in body:
            await self.second_document.wait()
        else:
            self.second_document.set()

    async def evaluate(self, js):
        title = re.search(r'<title>(.*?)</title>', self.body).group(1)
        return {'title': title, 'content': title, 'meta': {}}


class LoginPage:
    def __init__(self, body, status=200, url='https://www.faxin.cn/alllibsearch/SearchResult.aspx', request=None):
        self.body = body
        self.status = status
        self.url = url
        self.request = request

    async def goto(self, url, **kwargs):
        if self.request:
            response = await self.request.get(url, **kwargs)
            self.body, self.status, self.url = response.body, response.status, response.url
        return Response(self.url, self.body, self.status)

    async def wait_for_timeout(self, milliseconds):
        pass

    async def evaluate(self, js):
        return False

    async def content(self):
        return self.body

    async def close(self):
        pass

    def locator(self, selector):
        root = html.document_fromstring(self.body or '<html></html>')
        if 'loginForm' in selector:
            nodes = root.xpath('.//form[@id="loginForm"] | .//*[@class="login-box"] | .//input[@type="password"]')
        else:
            nodes = root.xpath('.//a | .//button | .//input[@type="button" or @type="submit"]')
        return LocatorList([Control(node, root) for node in nodes])


class LocatorList:
    def __init__(self, controls):
        self.controls = controls

    async def all(self):
        return self.controls


class Control:
    """Fixture-side browser visibility, including the CSS case under test."""
    def __init__(self, node, root):
        self.node = node
        css = '\n'.join(root.xpath('//style/text()'))
        self.hidden_classes = set(re.findall(r'\.([\w-]+)\s*\{\s*display\s*:\s*none', css, re.I))

    async def is_visible(self):
        for node in [self.node, *self.node.iterancestors()]:
            style = re.sub(r'\s+', '', node.get('style', '').lower())
            if (node.tag in ('template', 'noscript') or 'hidden' in node.attrib
                    or node.get('type', '').lower() == 'hidden'
                    or 'display:none' in style or 'visibility:hidden' in style
                    or self.hidden_classes.intersection(node.get('class', '').split())):
                return False
        return True

    async def inner_text(self):
        return self.node.text_content()

    async def get_attribute(self, name):
        return self.node.get(name)
