// Preloaded only in the Pen CLI renderer subprocess.
const { createHash, randomUUID } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const directory = process.env.LOCAL_PEN_FONT_CACHE_DIR;
const missingResources = process.env.LOCAL_PEN_MISSING_RESOURCES;
const offline = process.env.LOCAL_PEN_OFFLINE === "1";
const previewImages = new Set(JSON.parse(process.env.LOCAL_PEN_PREVIEW_IMAGES || "[]"));
// The CJK font is about 17 MiB after the HTTP content encoding is removed.
const maxFontBytes = 24 * 1024 * 1024;
const maxCacheBytes = 64 * 1024 * 1024;
const maxAgeMs = 30 * 60 * 1000;
const fallbackMaxAgeMs = 48 * 60 * 60 * 1000;
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

function readFont(file, magic, ageLimit = maxAgeMs) {
    try {
        const stat = fs.statSync(file);
        if (Date.now() - stat.mtimeMs >= ageLimit || stat.size > maxFontBytes) return;
        const data = fs.readFileSync(file);
        if (data.subarray(0, 4).toString("latin1") === magic) return data;
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
        && Number(maxAge?.slice(8)) - age >= maxAgeMs / 1000
        && Number(response.headers.get("content-length") ?? "0") <= maxFontBytes;
}

async function saveFont(file, response, magic) {
    let temporary;
    try {
        const chunks = [];
        let size = 0;
        for await (const chunk of response.body) {
            size += chunk.length;
            if (size > maxFontBytes) return;
            chunks.push(chunk);
        }
        const data = Buffer.concat(chunks, size);
        if (data.subarray(0, 4).toString("latin1") !== magic) return;
        let occupied = 0;
        for (const entry of fs.readdirSync(directory)) {
            if (!/^[a-f0-9]{64}$/.test(entry)) continue;
            const candidate = path.join(directory, entry);
            const stat = fs.statSync(candidate);
            if (Date.now() - stat.mtimeMs >= fallbackMaxAgeMs) fs.unlinkSync(candidate);
            else if (candidate !== file) occupied += stat.size;
        }
        if (occupied + size > maxCacheBytes) return;
        temporary = path.join(directory, randomUUID());
        fs.writeFileSync(temporary, data, { flag: "wx", mode: 0o600 });
        fs.renameSync(temporary, file);
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
        const data = readFont(file, magic, offline ? fallbackMaxAgeMs : maxAgeMs);
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
            const fallback = !init?.signal?.aborted && readFont(file, magic, fallbackMaxAgeMs);
            if (fallback) return new Response(fallback, { headers: { "content-type": contentType } });
            if (missingResources && !init?.signal?.aborted) markMissingResource();
            throw error;
        } finally {
            clearTimeout(timer);
        }
        if (!response.ok) {
            const fallback = (response.status === 429 || response.status >= 500)
                && readFont(file, magic, fallbackMaxAgeMs);
            if (fallback) return new Response(fallback, { headers: { "content-type": contentType } });
            if (missingResources) markMissingResource();
        }
        if (cacheableResponse(response, url, contentType)) {
            // Preserve the CLI's original response while storing a complete copy.
            void saveFont(file, response.clone(), magic).catch(() => {});
        }
        return response;
    };
}
