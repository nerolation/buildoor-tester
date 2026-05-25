# buildoor patches

The replay pipeline drives blocks through buildoor's build path. That needs
two small additions to buildoor itself. They aren't in upstream yet, so we
ship them here as a patch + a new source file.

Apply from your buildoor checkout:

```bash
# 1. Engine-API client: tolerate `null` for slotNumber on pre-Amsterdam payloads.
#    geth returns "slotNumber": null on Prague/etc and the existing
#    hexutil.Uint64 unmarshal rejected it.
git apply /path/to/buildoor-tester/patches/01-engine-client-nullable-slotnumber.patch

# 2. New `simbuild` subcommand: a CL-less HTTP build endpoint that the
#    replay harness POSTs to. Just drop the file into cmd/.
cp /path/to/buildoor-tester/patches/cmd_simbuild.go cmd/simbuild.go

# 3. Build the binary. The webui's embed directive needs the static dir to
#    exist; if you haven't built the frontend, drop in a placeholder:
mkdir -p pkg/webui/static && touch pkg/webui/static/.placeholder
go build -o /tmp/buildoor .
```

Confirm:

```bash
/tmp/buildoor simbuild --help
```

`buildoor-tester/` then takes the binary path via `--buildoor /tmp/buildoor`.

## Why these specifically

- `01-engine-client-nullable-slotnumber.patch`: blocks built before Amsterdam
  carry no `slotNumber`. Geth marshals the field as `null` rather than omitting
  it, and `hexutil.Uint64.UnmarshalJSON` rejects `null`. Making the field a
  pointer in the JSON shape preserves zero/absent semantics for both directions.

- `cmd_simbuild.go`: introduces `buildoor simbuild`, a minimal subcommand that
  exposes buildoor's payload-build pipeline behind one HTTP endpoint
  (`POST /build`). It reuses `engine.Client.RequestPayloadBuild`,
  `engine.Client.GetPayloadRaw`, and `builder.ModifyPayloadExtraData` — so the
  same code paths run in the harness as in a real builder, just without the
  beacon-node/lifecycle plumbing.
