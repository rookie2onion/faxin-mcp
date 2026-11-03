"""Exercise the real MCP SDK and stdio transport without a browser or account."""
import os
import sys
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_discoverable_and_unsupported_features_are_errors(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, FAXIN_BASE_URL='https://www.faxin.cn')
        parameters = StdioServerParameters(command=sys.executable, args=[str(root / 'server.py')],
                                           cwd=str(root), env=env)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                self.assertEqual(len(tools.tools), 9)
                result = await session.call_tool('faxin_search_lalei', {'keyword': '合同'})
                self.assertTrue(result.isError)
                self.assertIn('主站暂不支持', result.content[0].text)
                self.assertNotIn('会话已过期', result.content[0].text)
                invalid = await session.call_tool('faxin_get_detail', {'gid': 'http://127.0.0.1/private'})
                self.assertTrue(invalid.isError)
                self.assertIn('法信 HTTPS', invalid.content[0].text)
                negative = await session.call_tool('faxin_search_law', {'keyword': '合同', 'max_results': -1})
                self.assertTrue(negative.isError)
                self.assertIn('Input validation', negative.content[0].text)
