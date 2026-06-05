# aeview

Fan code reviewers across multiple agent harnesses, then merge one deduplicated verdict.

A reviewer is a prompt (`REVIEWER.md`) plus a set of harness instances (an agent CLI +
model). aeview resolves reviewers, bundles the diff under review, runs every
`reviewer x harness` pair in parallel, deduplicates the findings, and writes a single
`report.json` with an exit code you can loop on (`0` approve / `1` needs-attention / `2` error).

> Status: early development. Increment 1 (the vertical slice) supports
> `aeview run --scope working-tree` against the `claude-code` harness.

## Install

```sh
uv tool install aeview
```

## Usage

```sh
aeview run --scope working-tree
```

## License

MIT
