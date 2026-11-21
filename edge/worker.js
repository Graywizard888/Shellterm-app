// Shellterm package mirror, a Cloudflare Worker.
//
// apt and the first-run download ask this address instead of github.com, which can be slow on some networks:
//   /apt-1 .. /apt-4 and /bootstrap   served from the R2 bucket (a copy made by the Packages workflow); a file that is
//                                      not in the bucket yet is fetched from the GitHub release instead
//   /apt-x11-1, /apt-x11-2, /apt-root-1  not copied; fetched from the GitHub release once and kept on Cloudflare's edge
// Same paths as the release downloads: /<release tag>/<file>.

const GITHUB = "https://github.com/Graywizard888/Shellterm-app/releases/download";
const R2_TAGS = new Set(["apt-1", "apt-2", "apt-3", "apt-4", "bootstrap"]);
const TAGS = new Set([...R2_TAGS, "apt-x11-1", "apt-x11-2", "apt-root-1"]);
// Package file names are sanitised to these characters by the Packages workflow (deb_file_name).
const PATH = /^\/([A-Za-z0-9-]+)\/([A-Za-z0-9._-]+)$/;
const INDEX = /^(Packages(\.gz|\.xz)?|Release|.*\.sha256)$/;
const INDEX_TTL = 300; // an index must follow the weekly run quickly
const FILE_TTL = 86400;

export function parseRange(range, size) {
  // Start and end (inclusive) of a single-range "bytes=" header, or null when it is not one we can serve.
  const m = /^bytes=(\d*)-(\d*)$/.exec(range ?? "");
  if (!m || (m[1] === "" && m[2] === "")) return null;
  let start;
  let end;
  if (m[1] === "") {
    start = Math.max(size - Number(m[2]), 0);
    end = size - 1;
  } else {
    start = Number(m[1]);
    end = m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1);
  }
  return start <= end && start < size ? { start, end } : null;
}

function plain(status, text, extra = {}) {
  return new Response(text, { status, headers: { "Content-Type": "text/plain; charset=utf-8", ...extra } });
}

async function fromBucket(request, env, tag, file, ttl) {
  const key = `${tag}/${file}`;
  const head = request.method === "HEAD";
  const rangeHeader = request.headers.get("Range");
  // Most requests (apt asks for whole files) cost one bucket call; only a ranged one needs the size first.
  const meta = head || rangeHeader ? await env.PACKAGES.head(key) : null;
  if ((head || rangeHeader) && !meta) return null;
  const span = meta && rangeHeader ? parseRange(rangeHeader, meta.size) : null;
  if (rangeHeader && !span) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${meta.size}` } });
  }
  const object = head
    ? meta
    : await env.PACKAGES.get(key, span ? { range: { offset: span.start, length: span.end - span.start + 1 } } : undefined);
  if (!object) return null;
  const total = meta ? meta.size : object.size; // a ranged get's own size may be the slice's
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("ETag", object.httpEtag);
  headers.set("Accept-Ranges", "bytes");
  headers.set("Cache-Control", `public, max-age=${ttl}`);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/octet-stream");
  if (span) {
    headers.set("Content-Range", `bytes ${span.start}-${span.end}/${total}`);
    headers.set("Content-Length", String(span.end - span.start + 1));
  } else {
    headers.set("Content-Length", String(total));
  }
  return new Response(head ? null : object.body, { status: span ? 206 : 200, headers });
}

async function fromGithub(request, ctx, tag, file, ttl) {
  const upstream = `${GITHUB}/${tag}/${encodeURIComponent(file)}`;
  const range = request.headers.get("Range");
  if (request.method === "HEAD") return fetch(upstream, { method: "HEAD", redirect: "follow" });
  const cache = caches.default;
  const hit = await cache.match(new Request(upstream, range ? { headers: { Range: range } } : undefined));
  if (hit) return hit;
  const response = await fetch(upstream, { redirect: "follow", headers: range ? { Range: range } : undefined });
  // Only a whole file is kept; a ranged answer, an error or a missing file is passed on as it is.
  if (range || response.status !== 200) return response;
  const out = new Response(response.body, response);
  out.headers.delete("Set-Cookie");
  out.headers.set("Cache-Control", `public, max-age=${ttl}`);
  out.headers.set("Accept-Ranges", "bytes");
  ctx.waitUntil(cache.put(new Request(upstream), out.clone()));
  return out;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return plain(405, "Method not allowed", { Allow: "GET, HEAD" });
    }
    let path;
    try {
      path = decodeURIComponent(new URL(request.url).pathname);
    } catch {
      return plain(400, "Bad path");
    }
    if (path === "/") return plain(200, "Shellterm package mirror\n");
    const match = PATH.exec(path);
    if (!match || !TAGS.has(match[1])) return plain(404, "Not found");
    const [, tag, file] = match;
    const ttl = INDEX.test(file) ? INDEX_TTL : FILE_TTL;
    if (R2_TAGS.has(tag) && env.PACKAGES) {
      const served = await fromBucket(request, env, tag, file, ttl);
      if (served) return served;
    }
    return fromGithub(request, ctx, tag, file, ttl);
  },
};
