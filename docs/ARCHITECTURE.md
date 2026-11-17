# Current architecture (v0.3 alpha)

The initial implementation is in `usync.py`, with a small CLI entry point.

```text
config.yaml + tokens.env + optional browser session
                   |
                   v
          CLI / sync_unit()
              /    |    \
             v     v     v
          Canvas   Ed   Echo360
              \    |    /
               v   v   v
            latest_dump/
                   |
      build_gpt_ready_tree()
                   |
               UNIT.zip
```

- **Canvas**: fetches accessible course metadata/content using the Canvas REST API and downloads file links.
- **Ed**: fetches user-visible lessons and discussions; optional Playwright browser discovery finds assets and workspaces.
- **Echo360**: uses a saved browser session to discover recordings and attempt transcript downloads.
- **Storage**: stages output, records source results in a manifest, and replaces the prior dump only after at least one source succeeds.
- **Packaging**: removes redundant renderings and selected diagnostics from the lean ZIP while preserving an omission audit.

## Intended future modules

`cli.py`, `config.py`, `clients/canvas.py`, `clients/ed.py`, `clients/echo360.py`, `storage.py`, `archive.py`, and `auth.py`. This is a roadmap, **not** the current on-disk structure. Split only as covered by regression tests.
