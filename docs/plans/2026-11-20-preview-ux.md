# Preview interaction and navigation implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver approved improvements 1, 2, 4 and 6: continuous revision display, responsive pan/zoom, stable persistent camera, and whole-canvas artboard navigation.

**Architecture:** Keep the existing native renderer and fixed task URLs. Use a bounded viewport snapshot during revision replacement; world-coordinate tiles share a camera transform, frame-scheduled requests and a bounded neighbor/LRU cache. Persist camera coordinates per document and tab. Extend native manifest with artboard geometry and an opaque document key. Do not restart existing previews.

**Tech Stack:** Python stdlib, native CLI 0.3.16 adapter, browser DOM/Canvas2D, requestAnimationFrame, local/session storage, isolated Playwright acceptance.

1. Add `tests/verify_pen_viewer.cjs` with independent regressions for revision continuity, continuous-pan requests, resize/reload camera stability, and searchable whole-canvas artboard navigation. Run against 1.21.0 to observe failures. Existing commands: `python3 -B -m unittest discover -s tests`, `node --test tests/test_pen_*.cjs`.
2. Modify `pen_live_preview.py` camera helpers and navigation controls, preserve the existing inline functions and legacy whole-image fallback. Add actual 100% control and per-document viewport storage. Extend `pen_native_renderer.mjs` / `pen_tiles.py` manifest geometry and document identity. Add API identity/geometry regression coverage.
3. Modify `pen_preview.js` to stage a new revision behind one viewport snapshot capped at eight million pixels. Preserve newest-request and compatibility guards. Use a shared world transform, animation-frame scheduling, visible-first neighbor prefetch and an LRU capped at 64 decoded tiles. Update scheduler tests for the frame loop.
4. Run isolated browser fixtures through delayed/failed and superseded revisions, storage failures, huge/different document bounds, 100% resizing, hot/cold navigation, and search safety. Run actual Pen CLI and MCP acceptance using copies of the fixed production snapshot and 30000-high sample.
5. Run independent code review, relevant complete tests, syntax and whitespace checks. Update README and bump local version to 1.22.0. Copy verified files to the canonical repository, leaving the current preview and source design untouched. No commit/push until separately requested.

## Review fixes: legacy navigation and tile seams

- Legacy artboard metadata includes positive numeric dimensions in exported coordinates. Unknown auto layout dimensions remain unknown; navigation locates that frame's origin at 100% rather than inventing mobile-frame dimensions.
- Independent DOM clipping produced visible gaps at fractional camera scales and alpha overlap under some browser clipping modes. Compose decoded tiles within the visible viewport using shared integer canvas clip edges. Preserve padded source mapping, renderer scale selection, revision staging, and LRU; do not overlap alpha tiles.
- Limit the live canvas to 16 MP and each side to 8192 pixels. Keep the existing 8 MP transition limit and release discarded backing stores. Neighbor prefetch completion does not repaint an unchanged frame.
- Browser regressions exercise 25%-800% camera scales at DPR 1, 1.5, 2 and 3, opaque and 50% alpha content, held revision transitions, large-viewport caps, actual legacy metadata and unknown auto sizing. All tests use fixtures or isolated copies, without restarting an existing preview.
