const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const { existsSync, mkdtempSync, readdirSync, rmSync, utimesSync, statSync, writeFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join, resolve } = require("node:path");
const { test } = require("node:test");

const preload = resolve(__dirname, "../pen_font_cache.cjs");
const font = "https://fonts.gstatic.com/s/notosanssc/v39/k3kXo84MPvpLmixcA63oeALhKYiJ-Q7m8w.ttf";
const icon = "https://unpkg.com/lucide-static@0.563.0/font/lucide.woff2";
const fontEntries = directory => readdirSync(directory).filter(name => /^[a-f0-9]{64}$/.test(name));

function useCache(run) {
    const directory = mkdtempSync(join(tmpdir(), "local-pen-test-fonts-"));
    try {
        run(directory);
    } finally {
        rmSync(directory, { recursive: true, force: true });
    }
}

function fetchInChild(directory, { url = font, options, headers = {}, status = 200, size = 5, fail = false, offline = false, maxBytes, childTimeout = 10000 } = {}) {
    const request = JSON.stringify({ url, options, headers, status, size, fail });
    const result = spawnSync(process.execPath, ["-e", `
        const {url, options, headers, status, size, fail} = ${request};
        let calls = 0;
        globalThis.fetch = async (_, requestOptions) => {
            calls++;
            if (fail) {
                require("node:assert/strict").ok(requestOptions.signal instanceof AbortSignal);
                throw new Error("Font host unavailable");
            }
            const tag = url.endsWith(".woff2") ? [119, 79, 70, 50] : [0, 1, 0, 0];
            const data = new Uint8Array(size);
            data.set(tag);
            data[size - 1] = 255;
            const response = new Response(data, { status, headers: {
                "content-type": url.endsWith(".woff2") ? "font/woff2" : "font/ttf",
                "cache-control": "public, max-age=31536000",
                "vary": "Accept-Encoding",
                ...headers,
            } });
            Object.defineProperty(response, "url", { value: url });
            return response;
        };
        require(${JSON.stringify(preload)});
        (async () => {
            const response = await fetch(url, options);
            const bytes = require("node:crypto").createHash("sha256").update(Buffer.from(await response.arrayBuffer())).digest("hex");
            console.log(JSON.stringify({ calls, bytes, status: response.status }));
        })().catch(error => { console.error(error); process.exitCode = 1; });
    `], {
        encoding: "utf8", timeout: childTimeout,
        env: { ...process.env, LOCAL_PEN_FONT_CACHE_DIR: directory,
               LOCAL_PEN_OFFLINE: offline ? "1" : "0",
               LOCAL_PEN_FONT_CACHE_MAX_BYTES: String(maxBytes || 256 * 1024 * 1024),
               LOCAL_PEN_MISSING_RESOURCES: offline ? join(directory, "missing") : "" },
    });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
}

test("reuses the same public font bytes across CLI processes", () => useCache(directory => {
    for (const url of [font, icon]) {
        const miss = fetchInChild(directory, { url, size: url === font ? 17 * 1024 * 1024 : 5 });
        const hit = fetchInChild(directory, { url });
        assert.equal(miss.calls, 1);
        assert.deepEqual(hit, { ...miss, calls: 0 });
    }
    assert.equal(fontEntries(directory).length, 2);
}));

test("passes unrelated requests, personalized requests and invalid responses through", () => useCache(directory => {
    for (const request of [
        { url: "https://api.pen.dev/font.ttf" },
        { url: "https://fonts.gstatic.com/s/notosanssc/v39/font.ttf?key=1" },
        { url: "https://unpkg.com/lucide-static/font/lucide.woff2" },
        { options: { headers: { Range: "bytes=0-1" } } },
        { options: { cache: "reload" } },
        { headers: { "cache-control": "public, no-store, max-age=31536000" } },
        { headers: { "cache-control": "public, max-age=10" } },
        { headers: { "set-cookie": "session=example" } },
        { headers: { vary: "User-Agent" } },
        { headers: { "content-type": "text/html" } },
        { status: 206 },
    ]) {
        assert.equal(fetchInChild(directory, request).calls, 1);
        assert.equal(fontEntries(directory).length, 0);
    }
    fetchInChild(directory);
    assert.equal(fetchInChild(directory, { options: { headers: { Range: "bytes=0-1" } } }).calls, 1);
}));

test("a missing cache directory leaves normal fetch available", () => useCache(directory => {
    assert.equal(fetchInChild(join(directory, "missing")).calls, 1);
}));

test("all render modes reuse validated versioned fonts without online revalidation", () => useCache(directory => {
    const original = fetchInChild(directory, { size: 8 });
    const [entry] = fontEntries(directory);
    const old = new Date(Date.now() - 90 * 24 * 60 * 60 * 1000);
    utimesSync(join(directory, entry), old, old);
    const expected = { ...original, calls: 0 };
    assert.deepEqual(fetchInChild(directory, { fail: true }), expected);
    assert.deepEqual(fetchInChild(directory, { status: 503 }), expected);
    assert.deepEqual(fetchInChild(directory, { status: 403 }), expected);
    assert.deepEqual(fetchInChild(directory, { size: 12 }), expected);
}));

test("an isolated renderer flags a missing public font without fetching it", () => useCache(directory => {
    const marker = join(directory, "missing");
    const result = spawnSync(process.execPath, ["-e", `
        globalThis.fetch = () => { process.exit(2); };
        require(${JSON.stringify(preload)});
        fetch(${JSON.stringify(font)}).then(() => process.exit(3)).catch(() => {});
    `], {
        env: { ...process.env, LOCAL_PEN_FONT_CACHE_DIR: directory, LOCAL_PEN_OFFLINE: "1",
                LOCAL_PEN_MISSING_RESOURCES: marker },
        encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
    assert.ok(existsSync(marker));
}));

test("the online preview tracks missing referenced images without treating unrelated requests as assets", () => useCache(directory => {
    const marker = join(directory, "missing");
    const imageKey = require("node:crypto").createHash("sha256").update("example.com/image.png").digest("hex");
    const result = spawnSync(process.execPath, ["-e", `
        const { existsSync } = require("node:fs");
        const assert = require("node:assert/strict");
        globalThis.fetch = async () => new Response(null, { status: 404 });
        require(${JSON.stringify(preload)});
        (async () => {
            await fetch("https://example.com/unrelated.json");
            assert.equal(existsSync(process.env.LOCAL_PEN_MISSING_RESOURCES), false);
            await fetch("https://example.com/image.png?asset=sample");
            assert.equal(existsSync(process.env.LOCAL_PEN_MISSING_RESOURCES), true);
        })().catch(error => { console.error(error); process.exitCode = 1; });
    `], {
        env: { ...process.env, LOCAL_PEN_FONT_CACHE_DIR: directory,
                LOCAL_PEN_PREVIEW_IMAGES: JSON.stringify([imageKey]), LOCAL_PEN_MISSING_RESOURCES: marker },
        encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
}));

test("offline rendering reuses pinned font bytes after a weekend without network access", () => useCache(directory => {
    for (const url of [font, icon]) {
        const original = fetchInChild(directory, { url, size: 8 });
        const entry = require("node:crypto").createHash("sha256").update(url).digest("hex");
        const old = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);
        utimesSync(join(directory, entry), old, old);
        const hit = fetchInChild(directory, { url, offline: true, fail: true });
        assert.deepEqual(hit, { ...original, calls: 0 });
        assert.equal(existsSync(join(directory, "missing")), false);
    }
}));

test("saving another font keeps older valid fonts when capacity is available", () => useCache(directory => {
    const original = fetchInChild(directory, { size: 8 });
    const entry = require("node:crypto").createHash("sha256").update(font).digest("hex");
    const old = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);
    utimesSync(join(directory, entry), old, old);
    fetchInChild(directory, { url: icon });
    assert.ok(existsSync(join(directory, entry)), "A new download removed a usable old font");
    assert.deepEqual(fetchInChild(directory, { offline: true, fail: true }), { ...original, calls: 0 });
}));

test("font cache evicts only the oldest entries needed to stay within its capacity", () => useCache(directory => {
    const hash = url => require("node:crypto").createHash("sha256").update(url).digest("hex");
    const urls = [40,41,42].map(v => font.replace("/v39/", `/v${v}/`));
    for (const url of urls) fetchInChild(directory, { url, size: 20 * 1024 * 1024, maxBytes: 64 * 1024 * 1024 });
    urls.forEach((url,i) => {
        const age = new Date(Date.now() - (7-i) * 24 * 60 * 60 * 1000);
        utimesSync(join(directory,hash(url)),age,age);
    });
    const next = font.replace("/v39/", "/v43/");
    fetchInChild(directory, { url: next, size: 10 * 1024 * 1024, maxBytes: 64 * 1024 * 1024 });
    assert.equal(existsSync(join(directory,hash(urls[0]))),false);
    for (const url of [...urls.slice(1),next]) assert.ok(existsSync(join(directory,hash(url))));
    const total = fontEntries(directory).reduce((bytes,file) => bytes + statSync(join(directory,file)).size,0);
    assert.ok(total <= 64 * 1024 * 1024);
}));


test("new font versions download separately without replacing existing versions", () => useCache(directory => {
    const first = fetchInChild(directory, { size: 8 });
    const next = font.replace("/v39/", "/v40/");
    const second = fetchInChild(directory, { url: next, size: 12 });
    assert.equal(second.calls,1);
    assert.notEqual(first.bytes,second.bytes);
    assert.deepEqual(fetchInChild(directory, { fail: true }), { ...first, calls: 0 });
    assert.equal(fontEntries(directory).length,2);
}));

test("a corrupt stored font is downloaded again and repaired", () => useCache(directory => {
    const first = fetchInChild(directory, { size: 8 });
    const [entry] = fontEntries(directory);
    writeFileSync(join(directory,entry),Buffer.from("corrupt font"));
    assert.deepEqual(fetchInChild(directory, { size: 8 }),first);
    assert.deepEqual(fetchInChild(directory, { fail: true }),{...first,calls:0});
}));


test("an oversized uncached font response stays readable without blocking fetch", () => useCache(directory => {
    const result = fetchInChild(directory, { size: 25 * 1024 * 1024, childTimeout: 1500 });
    assert.equal(result.calls,1);
    assert.equal(result.status,200);
    assert.equal(fontEntries(directory).length,0);
}));
