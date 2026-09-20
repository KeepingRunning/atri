"""Deployment control flow with a fake Docker executable; no daemon is contacted."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMAGE_ID = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64
FAKE_DOCKER = r'''
import json
import os
from pathlib import Path
import sys

state = Path(os.environ["FAKE_DOCKER_STATE"])
args = sys.argv[1:]
with (state / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"args": args, "version": os.environ.get("ATRI_VERSION")}) + "\n")
mode = os.environ.get("FAKE_DOCKER_MODE", "success")
if args[0] == "compose":
    if "config" in args:
        sys.exit(0)
    if "ps" in args:
        print("b" * 64)
    elif "pull" in args:
        sys.exit(1 if mode == "pull_failed" else 0)
    elif "up" in args:
        rollback = any(a.endswith("/rollback.yaml") for a in args)
        phase = "rollback" if rollback else "new"
        (state / "phase").write_text(phase)
        if rollback:
            override = next(a for a in args if a.endswith("/rollback.yaml"))
            (state / "rollback.yaml").write_text(Path(override).read_text())
        if mode == "start_failed" and not rollback:
            sys.exit(1)
        if mode == "rollback_failed" and rollback:
            sys.exit(1)
    else:
        raise RuntimeError("unexpected compose call")
elif args[0] == "inspect":
    if args[2] == "{{.Image}}":
        print("sha256:" + "a" * 64)
    else:
        phase = (state / "phase").read_text()
        failed = mode in ("unhealthy", "rollback_failed") and phase == "new"
        if mode == "missing_healthcheck" and phase == "new":
            print("running missing")
        else:
            print("exited unhealthy" if failed else "running healthy")
else:
    raise RuntimeError("unexpected docker call")
'''


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.script = self.root / "deploy.sh"
        shutil.copyfile(ROOT / "scripts/deploy.sh", self.script)
        (self.root / "compose.yaml").write_text("services: {}\n")
        self.original = ("# version\nATRI_VERSION=0.1.0\n"
                         "SECRET=must-not-print\nLITERAL=$(touch should-not-exist)\n")
        self.env_file = self.root / ".env"
        self.env_file.write_text(self.original)
        self.env_file.chmod(0o600)
        docker = self.bin / "docker"
        docker.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        FAKE_DOCKER_STATE=str(self.root))
        self.env.pop("ATRI_VERSION", None)

    def run_deploy(self, *arguments, mode="success"):
        return subprocess.run(["bash", str(self.script), *arguments], cwd=self.root,
                              env=dict(self.env, FAKE_DOCKER_MODE=mode), capture_output=True,
                              text=True, timeout=10)

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_private_values_preserved(self, result):
        self.assertNotIn("must-not-print", result.stdout + result.stderr)
        self.assertFalse((self.root / "should-not-exist").exists())
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / ".deploy.lock").exists())
        self.assertFalse(list(self.root.glob(".deploy.*[0-9A-Za-z]")))

    def test_success_updates_only_atri_and_persists_version(self):
        result = self.run_deploy("0.1.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.env_file.read_text(), self.original.replace("0.1.0", "0.1.1"))
        mutations = [c for c in self.calls() if "pull" in c["args"] or "up" in c["args"]]
        self.assertEqual(len(mutations), 2)
        for call in mutations:
            self.assertEqual(call["args"][-1], "atri")
            self.assertEqual(call["version"], "0.1.1")
        self.assertIn("--no-deps", mutations[-1]["args"])
        record = json.loads((self.root / ".deploy-last.json").read_text())
        self.assertEqual(record["previous_image_id"], IMAGE_ID)
        self.assertEqual(record["target_image"], "ghcr.io/keepingrunning/atri:0.1.1")
        self.assert_private_values_preserved(result)

    def test_failed_new_container_restores_exact_old_image(self):
        for mode in ("unhealthy", "start_failed", "missing_healthcheck"):
            with self.subTest(mode=mode):
                result = self.run_deploy("0.1.1", mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.env_file.read_text(), self.original)
                self.assertIn(IMAGE_ID, (self.root / "rollback.yaml").read_text())
                self.assertEqual((self.root / "phase").read_text(), "rollback")
                self.assertIn("已恢复旧镜像", result.stderr)
                self.assert_private_values_preserved(result)

    def test_pull_failure_does_not_replace_container(self):
        result = self.run_deploy("0.1.1", mode="pull_failed")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("up" in c["args"] for c in self.calls()))
        self.assertEqual(self.env_file.read_text(), self.original)
        self.assert_private_values_preserved(result)

    def test_rollback_failure_is_not_reported_as_success(self):
        result = self.run_deploy("0.1.1", mode="rollback_failed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("自动回滚未恢复健康", result.stderr)
        self.assertEqual(self.env_file.read_text(), self.original)

    def test_bad_version_cannot_reach_docker(self):
        for args in ((), ("latest",), ("v0.1.1",), ("../0.1.1",), ("0.1.1; touch injected",),
                     ("0.1.1$(touch injected)",), ("0.1.1", "extra")):
            with self.subTest(args=args):
                result = self.run_deploy(*args)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.env_file.read_text(), self.original)
        self.assertFalse((self.root / "injected").exists())

    def test_duplicate_version_keys_and_missing_final_newline(self):
        self.env_file.write_text("ATRI_VERSION=0.1.0\nexport ATRI_VERSION=0.1.0\nOTHER=unchanged")
        result = self.run_deploy("0.2.0-rc.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.env_file.read_text(), "ATRI_VERSION=0.2.0-rc.1\nOTHER=unchanged")

    def test_write_failure_triggers_rollback(self):
        python = self.bin / "python3"
        python.write_text("#!/bin/sh\nexit 1\n")
        python.chmod(0o755)
        result = self.run_deploy("0.1.1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "phase").read_text(), "rollback")
        self.assertEqual(self.env_file.read_text(), self.original)

    def test_missing_version_is_appended_without_executing_other_values(self):
        original = "LITERAL=$(touch should-not-exist)"
        self.env_file.write_text(original)
        result = self.run_deploy("0.1.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.env_file.read_text(), original + "\nATRI_VERSION=0.1.1\n")
        self.assert_private_values_preserved(result)

    def test_concurrent_run_is_rejected(self):
        (self.root / ".deploy.lock").mkdir()
        result = self.run_deploy("0.1.1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.root / ".deploy.lock").exists())


if __name__ == "__main__":
    unittest.main()
