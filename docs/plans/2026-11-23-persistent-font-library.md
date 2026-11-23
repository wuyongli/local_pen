# Persistent font library implementation plan

**Goal:** Download a versioned public font once and reuse it across tasks, AI sessions and render entry points.

**Architecture:** Keep the existing URL whitelist, response privacy checks, byte validation and offline isolation. Store shared fonts in the user's persistent application data directory, migrate valid legacy temporary-cache entries without deleting originals, and apply a configurable bounded capacity (256 MiB by default). Both online and offline entry points use valid pinned-version bytes before fetching. Do not restart existing preview services or modify design originals.

**Files:** `pen_font_library.py`, `pen_render.py`, `pen_font_cache.cjs`, `tests/test_local_pen_mcp.py`, `tests/test_pen_font_cache.cjs`, `README.md`.

1. Add failing Python regressions for persistent directory selection across different temporary directories, safe legacy migration, private-directory/symlink refusal and configurable capacity. Add failing Node regressions for zero-fetch reuse after long storage in online mode, font-version separation, corrupt-cache repair and bounded eviction.
2. Extend the existing cache-directory helper: choose a private per-user application data path (or explicit absolute override), validate ownership/permissions, and copy only regular owned bounded valid font files from the old private cache with atomic publication. Keep older processes' temporary cache intact. Pass the capacity setting to the CLI preload.
3. Update the preload to reuse validated fixed-version font bytes regardless of age, while retaining existing request/response eligibility checks and missing-resource behavior. Apply the larger bounded capacity and use recent access to preserve frequently used fonts.
4. Run `python3 -B -m unittest discover -s tests -p 'test_*.py'`, `node --test tests/test_pen_*.cjs`, syntax and whitespace checks. Validate actual cache migration and fresh MCP PNG previews on a fixed product-design copy, without external font requests for existing entries.
5. Centralize migration and download publication in the stdlib Python helper under one POSIX file lock; add concurrent publication/migration capacity regressions. Cache hits do not start helper processes.
6. Complete an independent read-only review and copy verified files back only if the canonical working tree still matches its recorded baseline. Preserve earlier uncommitted repairs. Leave commit/push for explicit authorization.
