# Viewport tiles and reusable renderer implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep the task preview URL and editing workflow, serving clear viewport tiles through bounded offline renderer reuse.

**Architecture:** A per-preview Python supervisor owns a serial native CLI renderer, a full-scene manifest, and a bounded tile cache. Browser requests only visible tiles at a suitable pixel density. Old image routes remain available; incompatible CLI versions fail closed for the native adapter and use the existing preview route. No existing preview is restarted during development or validation.

**Tech Stack:** Python stdlib, Node registerHooks, Pen CLI 0.3.16 native scene renderer, browser HTML/CSS/JavaScript.

## Task 1: Renderer supervisor contracts
- Add `tests/test_pen_tiles.py` covering CLI compatibility, persistent process reuse, source reload, cancellation, timeout, crash recovery, memory/job/idle recycling, shared slot limits and cache budgets.
- Run `python3 -B -m unittest discover -s tests -p test_pen_tiles.py` and observe missing functionality.
- Add `pen_tiles.py` and `pen_native_renderer.mjs`. Stage copies in private temporary storage; rebase resources; deny renderer network; guard known CLI version and source structure. Acquire a bounded host-wide process lease and enforce RSS limits even during rendering. Close all owned child resources on timeout, cancellation or preview shutdown.

## Task 2: Preview protocol
- Extend tests for manifest/tile routes, bounds and input validation, ETags, stale revisions, unavailable renderer fallback, tile LRU and unchanged fixed URL.
- Modify `pen_live_preview.py` to add manifest and tile routes without removing canvas or title routes. Use native resolved layout and preserve scene draw order. Revision changes invalidate tiles. Asset stamps participate in revision checks.
- Test existing and new Python suites. Keep old running services untouched.

## Task 3: Browser viewport
- Add `pen_preview.js` containing tile scheduling, at most four in-flight requests, generation rejection, padded edges, bounded browser image memory, overview/zoom transitions and source-change updates that preserve camera position.
- Keep the original page as a fallback, reuse existing gesture and title interaction. Fetch local script only. Test real headless browser zoom/pan, no native-page zoom, save refresh, request bounds, latest-version handling and reload at the same URL.

## Task 4: Acceptance and delivery
- Use isolated task/state directories and copies of the fixed 57-board snapshot and synthetic 1200x30000 sample.
- Verify 2x detail fidelity against native reference, titles, shared components/overlaps, same URL and different MCP sessions, memory/idle recycle and PID reuse. Measure visible refresh including browser display separately from renderer timings.
- Run `python3 -B -m unittest discover -s tests`, `node --test tests/test_pen_font_cache.cjs`, Node syntax checks, `bash -n setup_local_pen.sh` and `git diff --check`.
- Update README installation/update files and resource limits. Integrate reviewed files into the canonical repository without restarting current preview or changing its saved address. Do not commit/push without a user request.
