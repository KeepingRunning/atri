"""Verify an isolated release image with fake credentials and no network access."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
HEALTH = """import json, urllib.request
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:28080/healthz', timeout=2) as r:
    data = json.load(r)
assert data['status'] == 'ok' and data['connected'] is False, data
print(json.dumps(data))
"""
CHECK_MCP = """import asyncio
from pathlib import Path
from atri_bot.config import Config
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
async def main():
    config = Config.load(Path('/app/config.toml'))
    config.read_personal_info()
    for name, server in config.mcp.servers.items():
        params = StdioServerParameters(command=server.command, args=server.args, env=server.env, cwd='/app')
        async with asyncio.timeout(20):
            async with Client(stdio_client(params), read_timeout_seconds=15, cache=None) as client:
                result = await client.list_tools()
                names = {tool.name for tool in result.tools}
                assert set(server.allowed_tools) <= names, (name, names)
                print('MCP ready:', name)
asyncio.run(main())
"""


def docker(*args, check=True, timeout=60):
    return subprocess.run(["docker", *args], text=True, capture_output=True, check=check, timeout=timeout)


def smoke(image):
    name = "atri-release-smoke-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="atri-container-smoke-") as directory:
        root = Path(directory).resolve()
        config = (ROOT / "deploy/config.toml.template").read_text(encoding="utf-8")
        config = config.replace('allowed_groups = []', 'allowed_groups = [1]')
        config = config.replace('self_id = ""', 'self_id = "99"')
        config = config.replace('token = ""', 'token = "smoke-token"', 1)
        config = config.replace('base_url = ""', 'base_url = "http://127.0.0.1:9/v1"', 1)
        config = config.replace('model = ""', 'model = "smoke-model"', 1)
        config = config.replace('api_key = ""', 'api_key = "smoke-key"', 1)
        (root / "config.toml").write_text(config, encoding="utf-8")
        (root / "data").mkdir()
        try:
            docker("run", "--detach", "--init", "--network", "none", "--name", name,
                   "--mount", f"type=bind,src={root / 'config.toml'},dst=/app/config.toml,readonly",
                   "--mount", f"type=bind,src={root / 'data'},dst=/app/data", image)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                health = docker("exec", name, "python", "-c", HEALTH, check=False, timeout=10)
                if health.returncode == 0:
                    print("Container health:", health.stdout.strip())
                    break
                time.sleep(.5)
            else:
                raise RuntimeError("Container did not become healthy")
            check = docker("exec", name, "python", "-c", CHECK_MCP)
            print(check.stdout.strip())
            docker("stop", "--time", "30", name, timeout=40)
            state = json.loads(docker("inspect", "--format", "{{json .State}}", name).stdout)
            if state["ExitCode"] != 0 or state["OOMKilled"]:
                raise RuntimeError(f"Unclean container shutdown: {state}")
            logs = docker("logs", name)
            if "[停止] 服务已退出" not in logs.stdout + logs.stderr:
                raise RuntimeError("Missing graceful-shutdown confirmation")
            if "[请求开始]" in logs.stdout + logs.stderr:
                raise RuntimeError("Smoke test unexpectedly attempted a model request")
            print("Container, bundled MCPs and SIGTERM shutdown passed (network disabled).")
        except Exception:
            logs = docker("logs", name, check=False)
            print(logs.stdout + logs.stderr)
            raise
        finally:
            docker("rm", "--force", name, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    smoke(parser.parse_args().image)
