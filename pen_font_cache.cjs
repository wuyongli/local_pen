// Preloaded only in the Pen CLI renderer subprocess.
const { createHash, randomUUID } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const directory = process.env.LOCAL_PEN_FONT_CACHE_DIR;
const missingResources = process.env.LOCAL_PEN_MISSING_RESOURCES;
const offline = process.env.LOCAL_PEN_OFFLINE === "1";
const previewImages = new Set(JSON.parse(process.env.LOCAL_PEN_PREVIEW_IMAGES || "[]"));
// The CJK font is about 17 MiB after the HTTP content encoding is removed.
const maxFontBytes = 24 * 1024 * 1024;
const configuredBytes = Number(process.env.LOCAL_PEN_FONT_CACHE_MAX_BYTES);
const maxCacheBytes = Number.isSafeInteger(configuredBytes) && configuredBytes >= 32 * 1024 * 1024
    && configuredBytes <= 2048 * 1024 * 1024 ? configuredBytes : 256 * 1024 * 1024;
const minCacheLifetimeMs = 30 * 60 * 1000;
const downloadTimeoutMs = 4000;
const originalFetch = globalThis.fetch;
const fontTypes = {
    ttf: ["font/ttf", "\x00\x01\x00\x00"],
    otf: ["font/otf", "OTTO"],
    woff: ["font/woff", "wOFF"],
    woff2: ["font/woff2", "wOF2"],
};

function publicFont(input, init) {
    if (typeof input !== "string" && !(input instanceof URL)) return;
    if (init && (Object.keys(init).some(key => key !== "method" && key !== "signal")
        || (init.method && init.method !== "GET") || init.signal?.aborted)) return;
    const url = URL.canParse(input) ? new URL(input) : null;
    if (!url || url.protocol !== "https:" || url.port || url.username || url.password || url.search || url.hash) return;
    const google = url.hostname === "fonts.gstatic.com"
        && /^\/s\/[a-z0-9]+\/v\d+\/[\w-]+\.(ttf|otf|woff2?)$/.test(url.pathname);
    const lucide = url.hostname === "unpkg.com"
        && /^\/lucide-static@\d+\.\d+\.\d+\/font\/lucide\.woff2$/.test(url.pathname);
    if (google || lucide) return url;
}

function readFont(file, magic) {
    try {
        const stat = fs.lstatSync(file);
        if (!stat.isFile() || stat.size < 4 || stat.size > maxFontBytes) return;
        const data = fs.readFileSync(file);
        if (data.subarray(0, 4).toString("latin1") === magic) {
            // Coarse access tracking keeps frequently used fonts during capacity eviction.
            if (Date.now() - stat.mtimeMs > 60000) {
                try { fs.utimesSync(file, stat.atime, new Date()); } catch { /* Read-only entries remain usable. */ }
            }
            return data;
        }
    } catch {
        // A missing or unavailable cache should leave the CLI's fetch available.
    }
}

function cacheableResponse(response, url, contentType) {
    if (response.status !== 200 || response.url !== url.href || !response.body
        || response.headers.has("set-cookie") || response.headers.get("content-type") !== contentType) return false;
    const vary = response.headers.get("vary");
    if (vary && vary.toLowerCase() !== "accept-encoding") return false;
    const controls = (response.headers.get("cache-control") ?? "").toLowerCase().split(",").map(value => value.trim());
    const maxAge = controls.find(value => value.startsWith("max-age="));
    const age = Number(response.headers.get("age") ?? "0");
    return controls.includes("public") && !controls.some(value => /^no-(cache|store)|^private/.test(value))
        && Number(maxAge?.slice(8)) - age >= minCacheLifetimeMs / 1000
        && Number(response.headers.get("content-length") ?? "0") <= maxFontBytes;
}

async function saveFont(file, response, magic) {
    let temporary;
    try {
        const chunks = [];
        let size = 0;
        const reader = response.body.getReader();
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                size += value.length;
                if (size > maxFontBytes) {
                    // Awaiting tee cancellation would wait for the caller's branch,
                    // which cannot be consumed until this cache attempt returns.
                    void reader.cancel().catch(() => {});
                    return;
                }
                chunks.push(value);
            }
        } finally {
            reader.releaseLock();
        }
        const data = Buffer.concat(chunks, size);
        if (data.subarray(0, 4).toString("latin1") !== magic) return;
        temporary = path.join(directory, "font-download-" + randomUUID());
        fs.writeFileSync(temporary, data, { flag: "wx", mode: 0o600 });
        // Python is already required by local_pen. Its shared flock protects both
        // migration and publication without adding a native Node dependency.
        await new Promise((resolve, reject) => {
            const child = spawn(process.env.LOCAL_PEN_CACHE_PYTHON || "python3",
                ["-B", path.join(__dirname, "pen_font_library.py"), "store", directory,
                 temporary, path.basename(file), String(maxCacheBytes)], { stdio: "ignore" });
            const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("Font store timed out")); }, 5000);
            child.once("error", error => { clearTimeout(timer); reject(error); });
            child.once("exit", code => { clearTimeout(timer); code === 0 ? resolve() : reject(new Error("Font store failed")); });
        });
        temporary = undefined;
    } catch {
        // Cache failures must not affect an in-flight render.
    } finally {
        if (temporary) fs.rmSync(temporary, { force: true });
    }
}

function markMissingResource(kind = "asset") {
    fs.appendFileSync(missingResources, `${kind}\n`, { mode: 0o600 });
}

if (directory && originalFetch) {
    globalThis.fetch = async function (input, init) {
        const url = publicFont(input, init);
        if (!url) {
            if (!missingResources || init?.signal?.aborted) return originalFetch.call(this, input, init);
            const address = input instanceof Request ? input.url : input;
            const external = URL.canParse(address) ? new URL(address) : null;
            if (!external || !["http:", "https:"].includes(external.protocol)) {
                return originalFetch.call(this, input, init);
            }
            if (offline && !["api.pen.dev", "api.pencil.dev"].includes(external.hostname)) {
                markMissingResource();
            }
            const key = createHash("sha256").update(external.hostname + external.pathname).digest("hex");
            if (!previewImages.has(key)) return originalFetch.call(this, input, init);
            try {
                const response = await originalFetch.call(this, input, init);
                if (!response.ok) markMissingResource();
                return response;
            } catch (error) {
                if (!init?.signal?.aborted) markMissingResource();
                throw error;
            }
        }
        const [contentType, magic] = fontTypes[path.extname(url.pathname).slice(1)];
        const file = path.join(directory, createHash("sha256").update(url.href).digest("hex"));
        // Whitelisted URLs identify fixed font versions; all render paths share them.
        const data = readFont(file, magic);
        if (data) return new Response(data, { headers: { "content-type": contentType } });
        if (offline && missingResources) {
            markMissingResource("font");
            throw new Error("Public font is not available in the local cache");
        }

        const timeout = new AbortController();
        const timer = setTimeout(() => timeout.abort(), downloadTimeoutMs);
        const signal = init?.signal ? AbortSignal.any([init.signal, timeout.signal]) : timeout.signal;
        let response;
        try {
            response = await originalFetch.call(this, input, { ...init, signal });
        } catch (error) {
            const fallback = !init?.signal?.aborted && readFont(file, magic);
            if (fallback) return new Response(fallback, { headers: { "content-type": contentType } });
            if (missingResources && !init?.signal?.aborted) markMissingResource();
            throw error;
        } finally {
            clearTimeout(timer);
        }
        if (!response.ok) {
            const fallback = (response.status === 429 || response.status >= 500)
                && readFont(file, magic);
            if (fallback) return new Response(fallback, { headers: { "content-type": contentType } });
            if (missingResources) markMissingResource();
        }
        if (cacheableResponse(response, url, contentType)) {
            // Preserve the CLI's original response while storing a complete copy.
            await saveFont(file, response.clone(), magic);
        }
        return response;
    };
}
