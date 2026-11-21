import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";
import worker, { parseRange } from "./worker.js";

const GITHUB = "https://github.com/Graywizard888/Shellterm-app/releases/download";

/** A bucket holding `files` (key -> string), answering like R2: head, get and get with a range. */
function bucket(files) {
  const calls = [];
  const object = (key, bytes) => ({
    size: bytes.length,
    httpEtag: `"${key}"`,
    writeHttpMetadata(headers) {
      headers.set("Content-Type", "application/octet-stream");
    },
    body: new Blob([bytes]).stream(),
  });
  return {
    calls,
    async head(key) {
      calls.push(["head", key]);
      return key in files ? object(key, Buffer.from(files[key])) : null;
    },
    async get(key, options) {
      calls.push(["get", key, options?.range]);
      if (!(key in files)) return null;
      let bytes = Buffer.from(files[key]);
      if (options?.range) bytes = bytes.subarray(options.range.offset, options.range.offset + options.range.length);
      return object(key, bytes);
    },
  };
}

class FakeCache {
  store = new Map();
  async match(request) {
    return this.store.get(request.url)?.clone();
  }
  async put(request, response) {
    this.store.set(request.url, response);
  }
}

let cache;
let upstream;
let waiting;
const ctx = { waitUntil: (p) => waiting.push(p) };

beforeEach(() => {
  cache = new FakeCache();
  upstream = [];
  waiting = [];
  globalThis.caches = { default: cache };
  globalThis.fetch = async (url, init = {}) => {
    upstream.push({ url: String(url), method: init.method ?? "GET", range: init.headers?.Range });
    if (String(url).endsWith("/missing.deb")) return new Response("no", { status: 404 });
    const body = "0123456789";
    if (init.headers?.Range) return new Response("234", { status: 206 });
    return new Response(body, { status: 200, headers: { "Set-Cookie": "a=b" } });
  };
});

const get = (path, headers = {}, env = {}, method = "GET") =>
  worker.fetch(new Request(`https://mirror.example${path}`, { method, headers }), env, ctx);

describe("parseRange", () => {
  it("reads open, closed and suffix ranges", () => {
    assert.deepEqual(parseRange("bytes=2-4", 10), { start: 2, end: 4 });
    assert.deepEqual(parseRange("bytes=2-", 10), { start: 2, end: 9 });
    assert.deepEqual(parseRange("bytes=-3", 10), { start: 7, end: 9 });
    assert.deepEqual(parseRange("bytes=5-99", 10), { start: 5, end: 9 });
  });
  it("refuses what it cannot serve", () => {
    assert.equal(parseRange("bytes=10-", 10), null);
    assert.equal(parseRange("bytes=4-2", 10), null);
    assert.equal(parseRange("bytes=0-1,3-4", 10), null);
    assert.equal(parseRange("items=0-1", 10), null);
    assert.equal(parseRange(null, 10), null);
  });
});

describe("routing", () => {
  it("answers the root and refuses unknown paths and methods", async () => {
    assert.equal((await get("/")).status, 200);
    assert.equal((await get("/nope/a.deb")).status, 404);
    assert.equal((await get("/apt-1/../secret")).status, 404);
    assert.equal((await get("/apt-1/a b.deb")).status, 404);
    assert.equal((await get("/apt-1/%zz")).status, 400);
    const post = await get("/apt-1/a.deb", {}, {}, "POST");
    assert.equal(post.status, 405);
  });
});

describe("main lots and the bootstrap come from the bucket", () => {
  it("serves a whole file with one bucket call", async () => {
    const env = { PACKAGES: bucket({ "apt-1/a_1_aarch64.deb": "0123456789" }) };
    const res = await get("/apt-1/a_1_aarch64.deb", {}, env);
    assert.equal(res.status, 200);
    assert.equal(await res.text(), "0123456789");
    assert.equal(res.headers.get("Content-Length"), "10");
    assert.equal(res.headers.get("Cache-Control"), "public, max-age=86400");
    assert.equal(env.PACKAGES.calls.length, 1);
    assert.equal(upstream.length, 0);
  });

  it("serves a range as 206 with Content-Range", async () => {
    const env = { PACKAGES: bucket({ "bootstrap/b.zip": "0123456789" }) };
    const res = await get("/bootstrap/b.zip", { Range: "bytes=2-4" }, env);
    assert.equal(res.status, 206);
    assert.equal(await res.text(), "234");
    assert.equal(res.headers.get("Content-Range"), "bytes 2-4/10");
    assert.equal(res.headers.get("Content-Length"), "3");
  });

  it("refuses a range past the end", async () => {
    const env = { PACKAGES: bucket({ "bootstrap/b.zip": "0123456789" }) };
    const res = await get("/bootstrap/b.zip", { Range: "bytes=20-" }, env);
    assert.equal(res.status, 416);
    assert.equal(res.headers.get("Content-Range"), "bytes */10");
  });

  it("answers HEAD without a body", async () => {
    const env = { PACKAGES: bucket({ "apt-2/Packages.gz": "xyz" }) };
    const res = await get("/apt-2/Packages.gz", {}, env, "HEAD");
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("Content-Length"), "3");
    assert.equal(res.headers.get("Cache-Control"), "public, max-age=300");
    assert.equal(await res.text(), "");
  });

  it("falls back to GitHub when the bucket does not hold the file", async () => {
    const env = { PACKAGES: bucket({}) };
    const res = await get("/apt-1/new_1_aarch64.deb", {}, env);
    assert.equal(res.status, 200);
    assert.equal(upstream[0].url, `${GITHUB}/apt-1/new_1_aarch64.deb`);
  });

  it("falls back to GitHub when there is no bucket at all", async () => {
    const res = await get("/apt-3/x.deb", {}, {});
    assert.equal(res.status, 200);
    assert.equal(upstream.length, 1);
  });
});

describe("x11 and root come from GitHub and are kept", () => {
  it("fetches once, then serves from the edge cache", async () => {
    const first = await get("/apt-x11-1/g_1_aarch64.deb");
    assert.equal(await first.text(), "0123456789");
    assert.equal(first.headers.get("Set-Cookie"), null);
    assert.equal(first.headers.get("Cache-Control"), "public, max-age=86400");
    await Promise.all(waiting);
    const second = await get("/apt-x11-1/g_1_aarch64.deb");
    assert.equal(await second.text(), "0123456789");
    assert.equal(upstream.length, 1);
  });

  it("passes a range through and does not keep it", async () => {
    const res = await get("/apt-root-1/r.deb", { Range: "bytes=2-4" });
    assert.equal(res.status, 206);
    assert.equal(upstream[0].range, "bytes=2-4");
    await Promise.all(waiting);
    assert.equal(cache.store.size, 0);
  });

  it("does not keep a missing file", async () => {
    const res = await get("/apt-x11-2/missing.deb");
    assert.equal(res.status, 404);
    await Promise.all(waiting);
    assert.equal(cache.store.size, 0);
  });

  it("asks GitHub for HEAD without caching", async () => {
    await get("/apt-x11-1/g.deb", {}, {}, "HEAD");
    assert.equal(upstream[0].method, "HEAD");
  });
});
