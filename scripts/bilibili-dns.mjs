/**
 * Opt-in DNS for this MCP child only, never a system DNS or retry fallback.
 * Usage: ATRI_BILIBILI_DOH=1 node --import ./scripts/bilibili-dns.mjs ...
 * Only api.bilibili.com is resolved here; all other lookups remain unchanged.
 * Google JSON API: https://developers.google.com/speed/public-dns/docs/doh/json
 */
import dns from "node:dns";
import https from "node:https";
import { isIP } from "node:net";
import { syncBuiltinESMExports } from "node:module";

const TARGET = "api.bilibili.com";
const DEADLINE_MS = 10_000;
const MAX_BYTES = 65_536;
const originalLookup = dns.lookup;
let cache;
let inFlight;

export function isPublicIPv4(address) {
  if (typeof address !== "string" || isIP(address) !== 4) return false;
  const [a, b, c] = address.split(".").map(Number);
  return !(a === 0 || a === 10 || a === 127 || a >= 224
    || (a === 100 && b >= 64 && b <= 127)
    || (a === 169 && b === 254)
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && (b === 168 || (b === 0 && (c === 0 || c === 2)) || (b === 88 && c === 99)))
    || (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100)))
    || (a === 203 && b === 0 && c === 113));
}

function failure(code = "ENOTFOUND") {
  const error = new Error("Bilibili public DNS lookup failed; no resolver fallback was attempted");
  error.code = code;
  error.syscall = "getaddrinfo";
  error.hostname = TARGET;
  return error;
}

function bootstrap(hostname, options, callback) {
  if (typeof options === "function") [callback, options] = [options, {}];
  if (hostname !== "dns.google") return callback(failure());
  const address = { address: "8.8.8.8", family: 4 };
  if (options?.all) callback(null, [address]);
  else callback(null, address.address, address.family);
}

function resolvePublicAddresses() {
  if (cache && Date.now() < cache.expires) return Promise.resolve(cache.addresses);
  if (inFlight) return inFlight;
  inFlight = new Promise((resolve, reject) => {
    let settled = false;
    let request;
    const finish = (error, addresses) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) {
        request?.destroy();
        reject(error);
      } else resolve(addresses);
    };
    const timer = setTimeout(() => finish(failure("ETIMEOUT")), DEADLINE_MS);
    try {
      request = https.get(
        "https://dns.google/resolve?name=api.bilibili.com&type=A&edns_client_subnet=0.0.0.0%2F0",
        { lookup: bootstrap, family: 4, servername: "dns.google", rejectUnauthorized: true,
          headers: { "Accept": "application/dns-json", "Accept-Encoding": "identity" } },
        response => {
          if (response.statusCode !== 200 || (response.headers["content-encoding"] ?? "identity") !== "identity") {
            response.destroy();
            finish(failure());
            return;
          }
          let size = 0;
          const chunks = [];
          response.on("data", chunk => {
            size += chunk.length;
            if (size > MAX_BYTES) {
              response.destroy();
              finish(failure());
            } else chunks.push(chunk);
          });
          response.on("error", () => finish(failure()));
          response.on("end", () => {
            if (settled) return;
            try {
              const data = JSON.parse(Buffer.concat(chunks).toString("utf8"));
              if (data.Status !== 0 || data.TC || !Array.isArray(data.Answer) || data.Answer.length > 256) {
                throw failure();
              }
              const answers = data.Answer.filter(answer => answer?.type === 1);
              if (!answers.length || answers.some(answer => !isPublicIPv4(answer.data))) throw failure();
              const addresses = [...new Set(answers.map(answer => answer.data))].map(address => ({ address, family: 4 }));
              const ttl = Math.min(60, ...answers.map(answer =>
                Number.isFinite(answer.TTL) && answer.TTL > 0 ? answer.TTL : 0));
              cache = { addresses, expires: Date.now() + ttl * 1000 };
              finish(null, addresses);
            } catch {
              finish(failure());
            }
          });
        },
      );
      request.on("error", () => finish(failure()));
    } catch {
      finish(failure());
    }
  }).finally(() => { inFlight = undefined; });
  return inFlight;
}

function lookup(hostname, options, callback) {
  if (typeof hostname !== "string" || hostname.toLowerCase() !== TARGET) {
    return originalLookup.apply(this, arguments);
  }
  if (typeof options === "function") [callback, options] = [options, {}];
  if (typeof options === "number") options = { family: options };
  options ??= {};
  if (typeof callback !== "function") throw new TypeError("DNS lookup requires a callback");
  const family = options.family === "IPv4" ? 4 : options.family === "IPv6" ? 6 : options.family ?? 0;
  if (![0, 4, 6].includes(family)) throw new TypeError("Invalid DNS address family");
  if (family === 6) {
    queueMicrotask(() => callback(failure("ENODATA")));
    return;
  }
  let done = false;
  const signal = options.signal;
  const complete = (error, addresses) => {
    if (done) return;
    done = true;
    signal?.removeEventListener("abort", onAbort);
    if (error) callback(error);
    else if (options.all) callback(null, addresses.map(address => ({ ...address })));
    else callback(null, addresses[0].address, 4);
  };
  const onAbort = () => complete(failure("ABORT_ERR"));
  if (signal?.aborted) {
    queueMicrotask(onAbort);
    return;
  }
  signal?.addEventListener("abort", onAbort, { once: true });
  resolvePublicAddresses().then(addresses => complete(null, addresses), complete);
}

if (process.env.ATRI_BILIBILI_DOH === "1") {
  dns.lookup = lookup;
  // Keep named ESM imports consistent with the callback API used by net/undici.
  syncBuiltinESMExports();
}
