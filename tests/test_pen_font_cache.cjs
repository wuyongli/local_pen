const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const { existsSync, mkdtempSync, readdirSync, rmSync, utimesSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join, resolve } = require("node:path");
const { test } = require("node:test");

const preload = resolve(__dirname, "../pen_font_cache.cjs");
const font = "https://fonts.gstatic.com/s/notosanssc/v39/k3kXo84MPvpLmixcA63oeALhKYiJ-Q7m8w.ttf";
const icon = "https://unpkg.com/lucide-static@0.563.0/font/lucide.woff2";

function useCache(run) {
    const directory = mkdtempSync(join(tmpdir(), "local-pen-test-fonts-"));
    try {
        run(directory);
    } finally {
        rmSync(directory, { recursive: true, force: true });
    }
}

function fetchInChild(directory, { url = font, options, headers = {}, status = 200, size = 5, fail = false } = {}) {
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
        encoding: "utf8",
        env: { ...process.env, LOCAL_PEN_FONT_CACHE_DIR: directory },
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
    assert.equal(readdirSync(directory).length, 2);
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
        assert.equal(readdirSync(directory).length, 0);
    }
    fetchInChild(directory);
    assert.equal(fetchInChild(directory, { options: { headers: { Range: "bytes=0-1" } } }).calls, 1);
}));

test("a missing cache directory leaves normal fetch available", () => useCache(directory => {
    assert.equal(fetchInChild(join(directory, "missing")).calls, 1);
}));

test("uses a previously validated font when the public download fails", () => useCache(directory => {
    const original = fetchInChild(directory, { size: 8 });
    const [entry] = readdirSync(directory);
    const yesterday = new Date(Date.now() - 24 * 60 * 60 * 1000);
    utimesSync(join(directory, entry), yesterday, yesterday);
    assert.deepEqual(fetchInChild(directory, { fail: true }), original);
    assert.deepEqual(fetchInChild(directory, { status: 503 }), original);
    assert.equal(fetchInChild(directory, { status: 403 }).status, 403);
    assert.notEqual(fetchInChild(directory, { size: 12 }).bytes, original.bytes);
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
