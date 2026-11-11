"""隔离执行启动日志代码，不导入应用、日志模块或真实配置。"""

import ast
import dataclasses
import logging
from pathlib import Path
import sys
import types
import typing
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class StartupChannelLogRedactionTest(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
        logs = [
            n for n in ast.walk(run)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "info"
            and any(isinstance(s, ast.Constant) and isinstance(s.value, str)
                    and "[App] Starting channels:" in s.value for s in ast.walk(n))
        ]
        self.assertEqual(len(logs), 1)
        helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == "_startup_channel_log_entries"]
        self.code = compile(ast.Module(body=helpers + logs, type_ignores=[]), "app.py", "exec")

        # 使用真实 dataclass 定义，跳过渠道模块的全部导入和初始化。
        source = ast.parse((ROOT / "channel/channel_instances.py").read_text(encoding="utf-8"))
        cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "ChannelInstance")
        namespace = dict(vars(typing), dataclass=dataclasses.dataclass,
                         field=dataclasses.field, __name__=__name__)
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "channel_instances.py", "exec"), namespace)
        self.instance_type = namespace["ChannelInstance"]

    def render(self, entries):
        module = types.ModuleType("channel.channel_instances")
        module.ChannelInstance = self.instance_type
        logger = logging.Logger("isolated-startup")
        with patch.dict(sys.modules, {"channel.channel_instances": module}):
            with self.assertLogs(logger, level="INFO") as captured:
                exec(self.code, {"logger": logger, "channel_names": entries})
        return "\n".join(captured.output)

    def test_instances_log_identity_and_type_without_credentials(self):
        credentials = {"token": "fake-token-unique", "app_secret": "fake-secret-unique",
                       "future_credential": {"nested": "fake-nested-unique"}}
        entries = [self.instance_type("bot-one", "feishu", credentials=credentials),
                   self.instance_type("bot-two", "feishu", credentials=credentials), "web"]
        output = self.render(entries)
        for expected in ("bot-one", "bot-two", "feishu", "web"):
            self.assertIn(expected, output)
        for forbidden in ("credentials", "token", "app_secret", "future_credential",
                          "fake-token-unique", "fake-secret-unique", "fake-nested-unique"):
            self.assertNotIn(forbidden, output)
        self.assertIs(entries[0].credentials, credentials)

    def test_legacy_channels_and_empty_list(self):
        self.assertIn("['terminal', 'web']", self.render(["terminal", "web"]))
        self.assertIn("Starting channels: []", self.render([]))


if __name__ == "__main__":
    unittest.main()
