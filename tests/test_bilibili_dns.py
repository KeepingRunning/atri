"""Exercise the optional MCP Node DNS preload with fully mocked HTTPS/DNS."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = (ROOT / "scripts" / "bilibili-dns.mjs").as_uri()
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is optional; required only for Bilibili MCP")
class BilibiliDNSTests(unittest.TestCase):
    def run_node(self, source):
        result = subprocess.run([NODE, "--input-type=module", "-"], input=source,
                                text=True, capture_output=True, timeout=10, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "", "Preload must not contaminate MCP stdout")

    def test_disabled_preload_preserves_dns_function(self):
        self.run_node(f"""
import assert from 'node:assert/strict';
import dns from 'node:dns';
const original = dns.lookup;
delete process.env.ATRI_BILIBILI_DOH;
await import({MODULE!r});
assert.equal(dns.lookup, original);
""")

    def fixture(self, body, checks, *, status=200, encoding="identity"):
        return f"""
import assert from 'node:assert/strict';
import dns from 'node:dns';
import https from 'node:https';
import {{EventEmitter}} from 'node:events';
let queries = 0;
let originalCalls = 0;
dns.lookup = function(host, options, callback) {{
  originalCalls++;
  if(typeof options==='function') callback=options;
  callback(null,'127.0.0.1',4);
}};
https.get = (url, options, callback) => {{
  queries++;
  assert.equal(url,'https://dns.google/resolve?name=api.bilibili.com&type=A&edns_client_subnet=0.0.0.0%2F0');
  assert.equal(options.headers['Accept-Encoding'],'identity');
  assert.equal(options.servername,'dns.google');
  assert.equal(options.rejectUnauthorized,true);
  assert.equal(options.headers.Cookie,undefined);
  options.lookup('dns.google',{{all:true}},(error,addresses)=>{{
    assert.equal(error,null);assert.deepEqual(addresses,[{{address:'8.8.8.8',family:4}}]);
  }});
  const request = new EventEmitter();
  request.destroy = () => {{}};
  queueMicrotask(()=>{{
    const response = new EventEmitter();
    response.statusCode = {status};
    response.headers = {{'content-encoding':{encoding!r}}};
    response.destroy = () => {{}};
    callback(response);
    response.emit('data',Buffer.from({body}));
    response.emit('end');
  }});
  return request;
}};
process.env.ATRI_BILIBILI_DOH='1';
await import({MODULE!r});
const lookup = (host, options={{}}) => new Promise((resolve,reject)=>{{
  dns.lookup(host,options,(error,address,family)=>error?reject(error):resolve({{address,family}}));
}});
{checks}
"""

    def test_pins_public_ipv4_cache_and_does_not_change_other_hosts(self):
        body = "JSON.stringify({Status:0,Answer:[{type:5,data:'cdn.bilibili.com.'},{type:1,data:'8.8.4.4',TTL:60}]})"
        self.run_node(self.fixture(body, """
assert.deepEqual(await lookup('api.bilibili.com'),{address:'8.8.4.4',family:4});
assert.deepEqual(await lookup('api.bilibili.com',{family:'IPv4'}),{address:'8.8.4.4',family:4});
assert.deepEqual(await lookup('api.bilibili.com',{all:true}),{address:[{address:'8.8.4.4',family:4}],family:undefined});
assert.equal((await lookup('example.com')).address,'127.0.0.1');
assert.equal(queries,1);assert.equal(originalCalls,1);
await assert.rejects(lookup('api.bilibili.com',{family:6}),{code:'ENODATA'});
await assert.rejects(lookup('api.bilibili.com',{family:'IPv6'}),{code:'ENODATA'});
assert.equal(queries,1);assert.equal(originalCalls,1);
"""))

    def test_mixed_private_answer_rejected_without_system_fallback(self):
        body = "JSON.stringify({Status:0,Answer:[{type:1,data:'8.8.8.8',TTL:60},{type:1,data:'198.18.0.1',TTL:60}]})"
        self.run_node(self.fixture(body, """
await assert.rejects(lookup('api.bilibili.com'),{code:'ENOTFOUND'});
assert.equal(queries,1);assert.equal(originalCalls,0);
"""))

    def test_bad_status_body_compression_and_size_fail_without_fallback(self):
        for body, status, encoding in (
            ("'{}'", 302, "identity"),
            ("'not-json'", 200, "identity"),
            ("JSON.stringify({Status:2,Answer:[]})", 200, "identity"),
            ("JSON.stringify({Status:0,TC:true,Answer:[]})", 200, "identity"),
            ("'x'.repeat(65537)", 200, "identity"),
            ("'{}'", 200, "gzip"),
        ):
            with self.subTest(status=status, encoding=encoding, body=body):
                self.run_node(self.fixture(body, """
await assert.rejects(lookup('api.bilibili.com'),{code:'ENOTFOUND'});
assert.equal(queries,1);assert.equal(originalCalls,0);
""", status=status, encoding=encoding))

    def test_concurrent_lookups_share_query_and_aborted_lookup_does_not_retry(self):
        body = "JSON.stringify({Status:0,Answer:[{type:1,data:'8.8.8.8',TTL:60}]})"
        self.run_node(self.fixture(body, """
const controller=new AbortController();controller.abort();
await assert.rejects(lookup('api.bilibili.com',{signal:controller.signal}),{code:'ABORT_ERR'});
assert.equal(queries,0);
await Promise.all([lookup('api.bilibili.com'),lookup('api.bilibili.com')]);
assert.equal(queries,1);assert.equal(originalCalls,0);
"""))

    def test_public_ipv4_checks_exclude_private_fake_ip_and_reserved_ranges(self):
        self.run_node(f"""
import assert from 'node:assert/strict';
delete process.env.ATRI_BILIBILI_DOH;
const {{isPublicIPv4}} = await import({MODULE!r});
for(const address of ['0.1.2.3','10.1.2.3','127.0.0.1','100.64.0.1','169.254.1.1','172.16.0.1','192.0.0.1','192.0.2.1','192.168.1.1','198.18.0.1','198.51.100.1','203.0.113.1','224.0.0.1','8.8.8.8.evil','::1']) {{
  assert.equal(isPublicIPv4(address),false,address);
}}
assert.equal(isPublicIPv4('8.8.8.8'),true);
""")

    def test_total_dns_timeout_destroys_request_without_retry(self):
        self.run_node(f"""
import assert from 'node:assert/strict';
import dns from 'node:dns';
import https from 'node:https';
import {{EventEmitter}} from 'node:events';
let originalCalls=0, queries=0, destroyed=0;
dns.lookup=()=>{{originalCalls++;throw new Error('Unexpected fallback');}};
https.get=()=>{{queries++;const request=new EventEmitter();request.destroy=()=>destroyed++;return request;}};
const realTimer=globalThis.setTimeout;
globalThis.setTimeout=(callback,delay,...args)=>realTimer(callback,Math.min(delay,2),...args);
process.env.ATRI_BILIBILI_DOH='1';
await import({MODULE!r});
await assert.rejects(new Promise((resolve,reject)=>dns.lookup('api.bilibili.com',error=>error?reject(error):resolve())),{{code:'ETIMEOUT'}});
assert.equal(queries,1);assert.equal(originalCalls,0);assert.equal(destroyed,1);
""")


if __name__ == "__main__":
    unittest.main()
