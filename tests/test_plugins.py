"""The TypeScript plugins, executed for real under Bun against a stub daemon."""

import copy
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import pathsetup  # noqa: F401

from subcortex.config import DEFAULT_CONFIG
from subcortex.daemon import create_server
from subcortex.installers.base import bundled_plugin

BUN = shutil.which("bun")
BIG = "compiling module\\n".replace("\\n", "\n") * 800


class StubBackend:
    name = "stub"

    def predict(self, state, questions):
        if "simple" in questions:
            return {"answers": {"simple": {"noul": 0.99}}}
        return {"answers": {"needed": {"noul": 0.01}}}

    def available(self):
        return True, "stub"


OPENCODE_DRIVER = r"""
import plugin from "./plugin.ts"
const hooks = await plugin.server({})
const out: any = {}

const chat = {message: {id: "msg_1", sessionID: "ses_1"}, parts: [{id: "prt_a", sessionID: "ses_1", messageID: "msg_1", type: "text", text: "what is 2+2?"}]}
await hooks["chat.message"]({sessionID: "ses_1"}, chat)
out.parts = chat.parts

const big = BIG_TEXT
const bash = {title: "make", output: big, metadata: {exit: 0, truncated: false}}
await hooks["tool.execute.after"]({tool: "bash", sessionID: "ses_1", callID: "c1", args: {command: "make"}}, bash)
out.bash = bash.output

const read = {title: "read", output: big, metadata: {}}
await hooks["tool.execute.after"]({tool: "read", sessionID: "ses_1", callID: "c2", args: {}}, read)
out.read_untouched = read.output === big

const failed = {title: "make", output: big, metadata: {exit: 2}}
await hooks["tool.execute.after"]({tool: "bash", sessionID: "ses_1", callID: "c3", args: {}}, failed)
out.failed_untouched = failed.output === big

await hooks["experimental.text.complete"]({sessionID: "ses_1", messageID: "m", partID: "p"}, {text: "It is 4."})
const compaction = {context: [] as string[]}
await hooks["experimental.session.compacting"]({sessionID: "ses_1"}, compaction)
out.compaction = compaction.context

// garbage in, nothing thrown
for (const [name, fn] of Object.entries(hooks)) {
  await (fn as any)(undefined, undefined)
  await (fn as any)({}, {})
}
out.survived_garbage = true
console.log(JSON.stringify(out))
"""

AMP_DRIVER = r"""
import plugin from "./plugin.ts"
const handlers: Record<string, Function> = {}
const amp: any = {
  on: (name: string, fn: Function) => { handlers[name] = fn },
  system: {executor: {kind: "local"}},
  helpers: {shellCommandFromToolCall: (e: any) => e.tool === "Bash" ? {command: e.input.command} : null},
}
plugin(amp)
let head = "m1"
const ctx: any = {thread: {messages: async (opts: any) => opts.full
  ? [{role: "user", content: [{type: "text", text: "fix login"}]}, {role: "assistant", content: [{type: "text", text: "Fixed."}]}]
  : [{id: head}]}}
const out: any = {events: Object.keys(handlers).sort()}

await handlers["session.start"]({thread: {id: "T-1"}}, ctx)
out.start = await handlers["agent.start"]({thread: {id: "T-1"}, message: "what is 2+2?", id: 1}, ctx)
out.result = await handlers["tool.result"]({thread: {id: "T-1"}, tool: "Bash", toolUseID: "u1",
  input: {command: "make"}, status: "done", output: {output: BIG_TEXT, exitCode: 0}})
out.error_untouched = await handlers["tool.result"]({thread: {id: "T-1"}, tool: "Bash", toolUseID: "u2",
  input: {command: "make"}, status: "error", output: {output: BIG_TEXT}})
out.end = await handlers["agent.end"]({thread: {id: "T-1"}, message: "x", id: 2, status: "done", messages: []}, ctx)
await new Promise((r) => setTimeout(r, 300))  // the snapshot POST is fire-and-forget
head = "summary-1"  // a compaction changed the first visible message
out.after_compaction = await handlers["agent.start"]({thread: {id: "T-1"}, message: "and now?", id: 3}, ctx)
console.log(JSON.stringify(out))
"""


@unittest.skipUnless(BUN, "bun is not installed")
class PluginCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = create_server(0, cfg, backend_factory=lambda c, name=None: StubBackend())
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def run_plugin(self, tui, driver):
        work = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (work / "plugin.ts").write_text(bundled_plugin(tui, "subcortex.ts").replace("__SUBCORTEX_URL__", self.url))
        (work / "driver.ts").write_text(driver.replace("BIG_TEXT", json.dumps(BIG)))
        import os
        env = dict(os.environ, SUBCORTEX_DATA_DIR=self.tmp.name)
        env.pop("SUBCORTEX_URL", None)
        proc = subprocess.run([BUN, "run", "driver.ts"], cwd=work, capture_output=True, text=True,
                              timeout=60, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])


class TestOpenCodePlugin(PluginCase):
    def test_hooks_against_the_daemon(self):
        out = self.run_plugin("opencode", OPENCODE_DRIVER)
        hint = out["parts"][-1]
        self.assertEqual(len(out["parts"]), 2)
        self.assertTrue(hint["id"].startswith("prt_") and len(hint["id"]) == 30, hint["id"])
        self.assertEqual((hint["sessionID"], hint["messageID"], hint["synthetic"]), ("ses_1", "msg_1", True))
        self.assertIn("simple", hint["text"])
        self.assertIn("[subcortex: truncated", out["bash"])
        self.assertTrue(out["read_untouched"])
        self.assertTrue(out["failed_untouched"])
        self.assertEqual(len(out["compaction"]), 1)
        self.assertIn("user: what is 2+2?", out["compaction"][0])
        self.assertIn("assistant: It is 4.", out["compaction"][0])
        self.assertTrue(out["survived_garbage"])

    def test_single_default_export(self):
        source = bundled_plugin("opencode", "subcortex.ts")
        exports = [line for line in source.splitlines() if line.startswith("export ")]
        self.assertEqual(exports, ['export default { id: "subcortex", server }'])


class TestAmpPlugin(PluginCase):
    def test_hooks_against_the_daemon(self):
        out = self.run_plugin("amp", AMP_DRIVER)
        self.assertEqual(out["events"], ["agent.end", "agent.start", "session.start", "tool.result"])
        self.assertEqual(out["start"]["message"]["display"], False)
        self.assertIn("simple", out["start"]["message"]["content"])
        self.assertEqual(out["result"]["status"], "done")
        self.assertIn("[subcortex: truncated", out["result"]["output"]["output"])
        self.assertEqual(out["result"]["output"]["exitCode"], 0)
        self.assertIsNone(out.get("error_untouched"))
        self.assertIsNone(out.get("end"))
        self.assertIn("user: fix login", out["after_compaction"]["message"]["content"])


if __name__ == "__main__":
    unittest.main()
