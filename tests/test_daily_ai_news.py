import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]


def install_test_stubs() -> None:
    if "aiohttp" not in sys.modules:
        aiohttp_module = types.ModuleType("aiohttp")

        class ClientTimeout:
            def __init__(self, total=None):
                self.total = total

        class ClientSession:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        aiohttp_module.ClientTimeout = ClientTimeout
        aiohttp_module.ClientSession = ClientSession
        sys.modules["aiohttp"] = aiohttp_module

    if "astrbot.api" in sys.modules:
        return

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")

    class DummyLogger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class DummyFilter:
        @staticmethod
        def command(_name):
            def decorator(func):
                return func

            return decorator

    class MessageChain:
        def __init__(self):
            self.messages = []

        def message(self, text):
            self.messages.append(text)
            return self

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @classmethod
        def get_data_dir(cls, plugin_name=None):
            return Path(".")

    def register(*args, **kwargs):
        def decorator(cls):
            return cls

        return decorator

    event_module.filter = DummyFilter
    event_module.AstrMessageEvent = object
    event_module.MessageChain = MessageChain

    api_module.AstrBotConfig = dict
    api_module.logger = DummyLogger()
    api_module.event = event_module

    star_module.Context = object
    star_module.Star = Star
    star_module.register = register
    star_module.StarTools = StarTools

    astrbot_module.api = api_module

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module


def load_plugin_module():
    install_test_stubs()
    module_name = "daily_ai_news_plugin_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


plugin_main = load_plugin_module()

class FakeResponse:
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]):
        self.responses = responses
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, headers=None):
        self.requested_urls.append(url)
        return self.responses[url]


class DummyConfig(dict):
    def __init__(self, config_path: Path, initial: dict | None = None):
        super().__init__(initial or {})
        self.config_path = config_path
        self.save_calls = 0

    def save_config(self, replace_config: dict | None = None) -> None:
        if replace_config is not None:
            self.clear()
            self.update(replace_config)
        self.save_calls += 1
        self.config_path.write_text(
            json.dumps(self, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class DummyEvent:
    def __init__(self, unified_msg_origin: str):
        self.unified_msg_origin = unified_msg_origin

    def plain_result(self, text: str) -> str:
        return text


class DailyAINewsPluginTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        task = getattr(plugin_main, "_ACTIVE_SCHEDULER_TASK", None)
        if task is not None and not task.done():
            task.cancel()
        if hasattr(plugin_main, "_ACTIVE_SCHEDULER_TASK"):
            plugin_main._ACTIVE_SCHEDULER_TASK = None
        if hasattr(plugin_main, "_ACTIVE_SCHEDULER_OWNER"):
            plugin_main._ACTIVE_SCHEDULER_OWNER = None

    def create_plugin(
        self,
        temp_root: Path,
        *,
        data_dir: Path | None = None,
        config_values: dict | None = None,
        context: SimpleNamespace | None = None,
    ):
        data_dir = data_dir or (temp_root / "plugin_data")
        data_dir.mkdir(parents=True, exist_ok=True)
        config = DummyConfig(
            temp_root / "astrbot_plugin_daily_ai_news_config.json",
            {
                "push_hour": 8,
                "push_minute": 0,
                "rss_poll_interval": 600,
                "enable_ai_summary": True,
                "subscribed_groups": "",
                "subscribed_users": "",
                "command_subscribed_groups": "",
                "command_subscribed_users": "",
                **(config_values or {}),
            },
        )
        context = context or SimpleNamespace(
            send_message=AsyncMock(),
            get_using_provider=lambda: None,
        )

        with patch.object(plugin_main.StarTools, "get_data_dir", return_value=data_dir):
            plugin = plugin_main.DailyAINewsPlugin(context, config)

        return plugin, config, context, data_dir

    async def test_fetch_latest_article_accepts_daily_juya_rss_format(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            plugin, _, _, _ = self.create_plugin(temp_root)
            rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0"
                 xmlns:atom="http://www.w3.org/2005/Atom"
                 xmlns:content="http://purl.org/rss/1.0/modules/content/">
              <channel>
                <title>橘鸦 AI 早报</title>
                <link>https://daily.juya.uk/</link>
                <description>每日 AI 简讯</description>
                <atom:link href="https://daily.juya.uk/rss.xml" rel="self" type="application/rss+xml" />
                <item>
                  <title>2026-06-13</title>
                  <link>https://daily.juya.uk/issue-2/</link>
                  <description><![CDATA[<p>Claude Code 推出新的设置功能。</p>]]></description>
                  <content:encoded><![CDATA[<p>完整正文。</p>]]></content:encoded>
                  <guid>https://daily.juya.uk/issue-2/</guid>
                  <pubDate>Sat, 13 Jun 2026 00:45:53 +0000</pubDate>
                </item>
              </channel>
            </rss>"""
            session = FakeSession({plugin_main.RSS_URL: FakeResponse(200, rss_xml)})

            article = await plugin._fetch_latest_from_rss(session, {})

            self.assertIsNotNone(article)
            self.assertEqual(article["title"], "2026-06-13")
            self.assertEqual(article["link"], "https://daily.juya.uk/issue-2/")
            self.assertEqual(article["pub_date"], "Sat, 13 Jun 2026 00:45:53 +0000")
            self.assertIn("Claude Code", article["content"])

    async def test_fetch_latest_article_falls_back_to_homepage_when_rss_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            plugin, _, _, _ = self.create_plugin(temp_root)
            home_html = """
            <html><body>
              <h1><a href="https:&#x2F;&#x2F;daily.juya.uk&#x2F;issue-115&#x2F;">2026-06-08</a></h1>
              <a href="https://github.com/imjuya/juya-ai-daily/issues/115">read the source issue</a>
            </body></html>
            """
            article_html = """
            <html><body>
              <h1>AI Daily 2026-06-08</h1>
              <p>Seedance 2.0 and Codex daily selection news.</p>
            </body></html>
            """
            session = FakeSession(
                {
                    plugin_main.RSS_URL: FakeResponse(404, "not found"),
                    plugin_main.HOME_URL: FakeResponse(200, home_html),
                    "https://daily.juya.uk/issue-115/": FakeResponse(
                        200,
                        article_html,
                    ),
                }
            )

            with patch.object(
                plugin_main.aiohttp,
                "ClientSession",
                return_value=session,
            ):
                article = await plugin._fetch_rss_latest()

            self.assertIsNotNone(article)
            self.assertEqual(article["title"], "AI Daily 2026-06-08")
            self.assertEqual(
                article["link"],
                "https://daily.juya.uk/issue-115/",
            )
            self.assertIn("Seedance 2.0", article["content"])
            self.assertEqual(article["pub_date"], "")

    async def test_concurrent_instances_push_same_article_only_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            today = "2026-04-23"
            shared_data_dir = temp_root / "shared_plugin_data"
            shared_context = SimpleNamespace(
                send_message=AsyncMock(),
                get_using_provider=lambda: None,
            )
            entered_summary = 0
            summary_ready = asyncio.Event()
            article = {
                "title": f"AI日报 {today}",
                "link": "https://daily.juya.uk/issue-999/",
                "content": "x" * 200,
                "pub_date": "",
            }

            plugin_one, _, _, _ = self.create_plugin(
                temp_root / "instance_one",
                data_dir=shared_data_dir,
                config_values={"subscribed_groups": "123456"},
                context=shared_context,
            )
            plugin_two, _, _, _ = self.create_plugin(
                temp_root / "instance_two",
                data_dir=shared_data_dir,
                config_values={"subscribed_groups": "123456"},
                context=shared_context,
            )

            async def fake_fetch(self):
                return article

            async def fake_summary(self, article_data, article_date):
                nonlocal entered_summary
                entered_summary += 1
                if entered_summary >= 2:
                    summary_ready.set()
                try:
                    await asyncio.wait_for(summary_ready.wait(), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
                return "summary"

            with patch.object(
                plugin_main.DailyAINewsPlugin,
                "_fetch_rss_latest",
                new=fake_fetch,
            ), patch.object(
                plugin_main.DailyAINewsPlugin,
                "_get_or_create_summary",
                new=fake_summary,
            ):
                pushed = await asyncio.gather(
                    plugin_one._try_fetch_and_push(today),
                    plugin_two._try_fetch_and_push(today),
                )

            self.assertEqual(pushed, [True, False])
            self.assertEqual(shared_context.send_message.await_count, 1)

            sent_payload = json.loads(
                (shared_data_dir / "sent_news.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sent_payload["sent_dates"], [today])
            self.assertEqual(sent_payload["sent_links"], [article["link"]])

    async def test_initialize_backfills_command_subscription_fields(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            subscriptions = {
                "subscriptions": [
                    "default:GroupMessage:619114682",
                    "default:FriendMessage:562506516",
                    "suopeng:FriendMessage:o9cq80zqSziF-486w_ZDF8hXuOJE@im.wechat",
                ]
            }
            plugin, config, _, data_dir = self.create_plugin(temp_root)
            (data_dir / "subscriptions.json").write_text(
                json.dumps(subscriptions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            async def fake_schedule(self):
                return None

            with patch.object(
                plugin_main.DailyAINewsPlugin,
                "_schedule_loop",
                new=fake_schedule,
            ):
                await plugin.initialize()

            self.assertEqual(config["command_subscribed_groups"], "619114682")
            self.assertEqual(
                config["command_subscribed_users"],
                "562506516\nsuopeng:o9cq80zqSziF-486w_ZDF8hXuOJE@im.wechat",
            )
            self.assertGreaterEqual(config.save_calls, 1)

    async def test_command_subscribe_and_unsubscribe_sync_display_fields(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            plugin, config, _, _ = self.create_plugin(temp_root)

            group_event = DummyEvent("default:GroupMessage:619114682")
            friend_event = DummyEvent(
                "suopeng:FriendMessage:o9cq80zqSziF-486w_ZDF8hXuOJE@im.wechat",
            )

            group_results = [item async for item in plugin.cmd_subscribe(group_event)]
            friend_results = [item async for item in plugin.cmd_subscribe(friend_event)]

            self.assertTrue(group_results[-1].startswith("✅"))
            self.assertTrue(friend_results[-1].startswith("✅"))
            self.assertEqual(config["command_subscribed_groups"], "619114682")
            self.assertEqual(
                config["command_subscribed_users"],
                "suopeng:o9cq80zqSziF-486w_ZDF8hXuOJE@im.wechat",
            )

            unsub_results = [item async for item in plugin.cmd_unsubscribe(group_event)]

            self.assertTrue(unsub_results[-1].startswith("✅"))
            self.assertEqual(config["command_subscribed_groups"], "")
            self.assertEqual(
                config["command_subscribed_users"],
                "suopeng:o9cq80zqSziF-486w_ZDF8hXuOJE@im.wechat",
            )
