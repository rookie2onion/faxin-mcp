"""Parse faxin HTML locally, preserving article boundaries without running scripts."""
import re
from urllib.parse import urljoin, urlsplit

from lxml import html


_BLOCKS = {'address', 'article', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt',
           'h1', 'h2', 'h3', 'h4', 'header', 'li', 'ol', 'p', 'pre', 'section',
           'table', 'tbody', 'td', 'th', 'tr', 'ul'}
_SKIP = {'script', 'style', 'noscript', 'template'}


def document(body):
    return html.document_fromstring(body or '<html></html>',
                                    parser=html.HTMLParser(no_network=True))


def with_class(root, name):
    return root.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), $token)]',
                      token=f' {name} ')


def text_lines(element, include_hidden=False):
    chunks = []

    def walk(node):
        if not isinstance(node.tag, str):
            return
        tag = node.tag.lower()
        style = re.sub(r'\s+', '', (node.get('style') or '').lower())
        hidden = 'hidden' in node.attrib or 'display:none' in style or 'visibility:hidden' in style
        if tag in _SKIP or (hidden and not include_hidden):
            return
        if tag in _BLOCKS:
            chunks.append('\n')
        if node.text:
            chunks.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                chunks.append(child.tail)
        if tag in _BLOCKS:
            chunks.append('\n')

    walk(element)
    return '\n'.join(line for raw in ''.join(chunks).splitlines()
                     if (line := re.sub(r'\s+', ' ', raw).strip()))


def _visible(node):
    for parent in [node, *node.iterancestors()]:
        if not isinstance(parent.tag, str) or parent.tag.lower() in _SKIP:
            return False
        if parent.tag.lower() == 'input' and parent.get('type', '').lower() == 'hidden':
            return False
        style = re.sub(r'\s+', '', (parent.get('style') or '').lower())
        if 'hidden' in parent.attrib or 'display:none' in style or 'visibility:hidden' in style:
            return False
    return True


def is_login_page(url, body):
    if re.search(r'/(?:login|loginnew|loginpage)(?:\.aspx|/|$)', urlsplit(url).path, re.I):
        return True
    root = document(body)
    for node in root.xpath('.//form | .//input | .//div'):
        if not _visible(node):
            continue
        if (node.get('type', '').lower() == 'password'
                or node.get('id', '').lower() == 'loginform'
                or 'login-box' in node.get('class', '').split()):
            return True
    return any(hint in text_lines(root)[:3000] for hint in ('请输入账号', '请先登录'))


def parse_detail(body):
    root = document(body)
    titles = root.xpath('//title')
    title = text_lines(titles[0]) if titles else ''
    title = re.sub(r'\s*-\s*法信.*$', '', title).strip()
    candidates = [node for node in with_class(root, 'fulltext')
                  if 'box' in node.get('class', '').split()]
    candidates += with_class(root, 'content')
    candidates += root.xpath('.//*[@id="content"]')
    candidates += with_class(root, 'article-content')
    content = next((value for node in candidates if (value := text_lines(node))), '')
    if not content:
        bodies = root.xpath('//body')
        content = text_lines(bodies[0] if bodies else root)
        stop = content.find('法信简介')
        if stop > 100:
            content = content[:stop].strip()

    keys = {'制定机关', '文号', '发文字号', '效力等级', '效力级别', '公布日期',
            '施行日期', '时效性', '主题分类', '法律类别'}
    meta = {}
    for tab in with_class(root, 'js-sxTab'):
        lines = text_lines(tab, include_hidden=True).splitlines()
        for index, line in enumerate(lines):
            label = line.rstrip('：:').strip()
            if label in keys and index + 1 < len(lines) and lines[index + 1] not in keys:
                meta.setdefault(label, lines[index + 1])
            match = re.match(r'^([^：:]+)[：:]\s*(.+)$', line)
            if match and match[1].strip() in keys:
                meta.setdefault(match[1].strip(), match[2].strip())
    if not title and content:
        title = content.split('\n', 1)[0][:60]
    return {'title': title or '(无标题)', 'meta': meta, 'content': content}


def parse_outline(body, base_url):
    root = document(body)
    out = {'name': '', 'code': '', 'path': '', 'counts': {}, 'laws': []}
    navs = with_class(root, 'right_navation')
    if navs:
        labels = navs[0].xpath('.//label')
        match = re.match(r'^([A-Z]\d+(?:\.[A-Z]+\d+)*)([\s\S]*)$',
                         text_lines(labels[0]) if labels else '')
        if match:
            out.update(code=match[1], name=match[2].strip())
        paths = [line for line in text_lines(navs[0]).splitlines() if '>' in line and '[' not in line]
        if paths:
            out['path'] = re.sub(r'\s*>\s*', ' > ', paths[-1]).strip()
    for item in with_class(root, 'navation_item'):
        match = re.match(r'^([\s\S]*?)\s*([0-9]+\+?)$', text_lines(item))
        if match:
            key = re.sub(r'\s+', '', match[1])
            if key:
                out['counts'][key] = match[2]
    for item in with_class(root, 'content_item'):
        lines = text_lines(item).splitlines()
        if not lines:
            continue
        links = item.xpath('.//a[contains(@href, "/lib/")]')
        link = links[0] if links else None
        out['laws'].append({
            'law': text_lines(link) if link is not None else lines[0],
            'url': urljoin(base_url, link.get('href', '')) if link is not None else '',
            'excerpt': ' '.join(lines[1:])[:300],
        })
    return out
