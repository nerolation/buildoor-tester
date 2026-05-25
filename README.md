# buildoor-tester

Replay Ethereum execution-spec test fixtures through
[buildoor](https://github.com/ethpandaops/buildoor) and check that the blocks
it builds match what the fixture expects.

Block-level Access List (EIP-7928) correctness tests and worst-case opcode
saturation benchmarks live in the
[execution-specs](https://github.com/ethereum/execution-specs) tree. This
harness takes a filled fixture, boots a per-fixture geth EL with the fixture's
exact genesis, drives the fixture's pre-signed transactions through buildoor's
build pipeline, and diffs the produced payload against the expected one.

## What's in the box

```
buildoor-tester/
├── pyproject.toml             # uv project for the `eest-replay` CLI
├── src/eest_replay/
│   ├── cli.py                 # `eest-replay run <fixture-or-dir>`
│   ├── fixture.py             # BlockchainEngineFixture loading + discovery
│   ├── chainspec.py           # fixture.pre + genesis → geth genesis.json
│   ├── el.py                  # per-fixture geth lifecycle (Docker)
│   ├── buildoor_client.py     # spawns `buildoor simbuild`, POSTs /build
│   ├── runner.py              # bootstrap → submit txs → build → diff → advance
│   └── report.py              # aggregate batch results to JSON/Markdown
└── patches/
    ├── 01-engine-client-nullable-slotnumber.patch
    ├── cmd_simbuild.go        # new buildoor subcommand
    └── README.md              # how to apply
```

## Pipeline

```
just fill ...                  →  fixture JSON (one or many)
                                              │
                                              ▼
              ┌────────────── eest-replay (Python) ────────────────┐
              │  per fixture:                                       │
              │    write genesis.json from fixture.pre + genesis    │
              │    boot geth (Docker) with that chainspec           │
              │    spawn `buildoor simbuild` against geth's authrpc │
              │    fcu(genesis) bootstrap                           │
              │    for each block in fixture:                       │
              │       eth_sendRawTransaction(*fixture.transactions) │
              │       POST /build → buildoor builds via Engine API  │
              │       diff built payload vs fixture.expected        │
              │       newPayload(expected) + fcu(expected.hash)     │
              │    tear down                                        │
              └─────────────────────────────────────────────────────┘
                         │                              │
                         ▼                              ▼
                  ┌──────────┐                   ┌──────────┐
                  │  geth    │  ◄── Engine API ─►│ buildoor │
                  │  (EL)    │                   │ simbuild │
                  └──────────┘                   └──────────┘
```

What matches exactly: genesis hash, state_root, receipts_root, gas_used,
logs_bloom, transactions, block_access_list, withdrawals, fee_recipient,
slot_number, timestamp, prev_randao.

What is ignored by design (mirroring upstream `test_via_build.py`):
`gas_limit` (EL chooses its own via EIP-1559), `extra_data` (buildoor
tags blocks with `buildoor/`), and `block_hash` (transitively depends
on the previous two).

## Prerequisites

- **Docker** — used to run geth in isolation per fixture.
- **uv** — Python package manager. The Python project picks up
  `execution_testing` as an editable install of the local execution-specs
  checkout.
- **Go ≥ 1.25** — to build buildoor with the `simbuild` subcommand.
- **git** — repos checked out as siblings.

No host install of `geth`, `evm`, or `reth` is required. Filling uses the
EELS-native Python t8n (`ethereum-spec-evm`, shipped by execution-specs).
Replay uses `ethpandaops/geth:bal-devnet-2` via Docker.

> [!NOTE]
> Reth ≤ 2.2 returns "Unsupported fork" on Amsterdam payloads, so this
> harness uses geth (the `bal-devnet-2` image specifically, which has
> full EIP-7928 support). Reth will work as soon as it picks up Amsterdam.

## Setup

Clone the three repos as siblings. The path
`../execution-specs/packages/testing` is referenced from
`pyproject.toml`, so the relative layout matters:

```bash
mkdir builder && cd builder

git clone https://github.com/ethereum/execution-specs.git
git clone https://github.com/ethpandaops/buildoor.git
git clone git@github.com:nerolation/buildoor-tester.git
```

Apply the buildoor changes and build the binary (details in
[`patches/README.md`](patches/README.md)):

```bash
cd buildoor
git apply ../buildoor-tester/patches/01-engine-client-nullable-slotnumber.patch
cp ../buildoor-tester/patches/cmd_simbuild.go cmd/simbuild.go
mkdir -p pkg/webui/static && touch pkg/webui/static/.placeholder
go build -o /tmp/buildoor .
cd ..
```

Install the harness:

```bash
cd buildoor-tester
uv sync
```

Pre-pull the Docker image (one-time, ~60 MB):

```bash
docker pull ethpandaops/geth:bal-devnet-2
```

## Fill a fixture

From the `execution-specs` checkout:

```bash
cd ../execution-specs
uv sync

# Block Access List (Amsterdam):
uv run fill \
    --until Amsterdam \
    --output=.just/oneshot/fixtures \
    --skip-index --clean \
    -k "test_bal_nonce_changes" \
    tests/amsterdam/eip7928_block_level_access_lists/test_block_access_lists.py

# Benchmark saturation (Prague, 1M gas per case):
uv run fill \
    --include-benchmark --fork Prague --gas-benchmark-values 1 \
    --output=.just/oneshot/fixtures \
    --skip-index --clean \
    -k "blockchain_test_engine" \
    tests/benchmark/compute/instruction/test_arithmetic.py
```

> [!IMPORTANT]
> Do **not** pass `--evm-bin=...` for these fills. The default EELS-native
> t8n is used implicitly and it's the one that ships `-opcode.count`
> support. Passing a `--evm-bin` path triggers a binary-detection code
> path that doesn't recognize the EELS binary.

## Run the harness

Single fixture, builder = direct Engine API (no buildoor):

```bash
cd ../buildoor-tester
uv run eest-replay run \
    ../execution-specs/.just/oneshot/fixtures/blockchain_tests_engine/.../bal_nonce_changes.json
```

Single fixture, **builder = buildoor**:

```bash
uv run eest-replay run \
    ../execution-specs/.just/oneshot/fixtures/blockchain_tests_engine/.../bal_nonce_changes.json \
    --buildoor /tmp/buildoor
```

Whole directory (walks `blockchain_tests_engine/**/*.json`):

```bash
uv run eest-replay run \
    ../execution-specs/.just/oneshot/fixtures \
    --buildoor /tmp/buildoor \
    --report-md /tmp/report.md \
    --report-json /tmp/report.json
```

Useful flags:

| flag                          | purpose                                                                 |
|-------------------------------|-------------------------------------------------------------------------|
| `--buildoor <path>`           | route builds through `buildoor simbuild` (omit for direct Engine API)   |
| `--payload-build-time <sec>`  | how long the EL is given to build before `getPayload` (default 1.0 s)   |
| `--work-dir <path>`           | where per-fixture genesis/JWT/datadir live (default: a fresh tempdir)   |
| `--report-md <path>`          | write Markdown summary                                                  |
| `--report-json <path>`        | write JSON summary                                                      |
| `--stop-on-first-failure`     | exit at the first mismatch / error                                      |
| `-v` / `-vv`                  | progressively louder logging                                            |

## Architecture notes

### Per-fixture chainspec

`chainspec.py` converts the fixture's `pre` allocation and `genesisBlockHeader`
into a geth-compatible `genesis.json`. Two non-obvious bits:

- Forks are only activated up to the fixture's target fork. Turning on
  Amsterdam for a Prague fixture causes geth's `bal-devnet-2` to expect
  `blockAccessListHash` / `slotNumber` in the genesis header that the Prague
  fixture doesn't carry, so init silently fails.
- Amsterdam fixtures include `blockAccessListHash` and `slotNumber` in the
  genesis header. Without those, geth re-decodes its own genesis and bails
  with `rlp: input string too short for common.Hash`.

### Per-fixture EL

`el.py` boots `ethpandaops/geth:bal-devnet-2` in Docker. A few flags we set
on purpose:

- `--miner.gasprice 1`, `--txpool.pricelimit 1`, `--gpo.ignoreprice 1`:
  fixture transactions can have arbitrarily low gas prices. Geth silently
  bumps a `0` value back to 1 gwei, so we use the lowest value that
  survives sanitization.
- `--miner.extradata 0x`: stops geth from tagging built blocks with its
  default version string, which would otherwise pollute `extra_data`.
- `--datadir.minfreedisk 0`: bypass geth's 2 GB free-disk safety check.
- `--nodiscover --maxpeers 0`: deterministic local-only chain.

### getPayload version

`runner.py` picks the `getPayload` version from the fixture's fork:

| fork      | getPayload |
|-----------|------------|
| Cancun    | V3         |
| Prague    | V4         |
| Osaka     | V5         |
| Amsterdam | V6         |

The fixture's `new_payload_version` / `forkchoice_updated_version` are honored
verbatim for the import-and-advance path.

### Chain advancement

Every fixture block does **build-and-diff → newPayload(expected) → fcu(expected.hash)**.
The chain follows the *expected* blocks, not the ones the builder produced.
This mirrors EEST's upstream `test_via_build.py` so cross-block state stays
correct even when the builder disagrees on a block.

### simbuild

`buildoor simbuild` is a tiny subcommand (`patches/cmd_simbuild.go`) that
connects only to the Engine API — no beacon node, no BLS signer, no
lifecycle. It exposes one endpoint:

```
POST /build
{
  "parent_hash": "0x…",
  "timestamp": "0xc",
  "prev_randao": "0x0…",
  "suggested_fee_recipient": "0x…",
  "parent_beacon_block_root": "0x…",
  "slot_number": "0x0",
  "target_gas_limit": "0x0",
  "withdrawals": []
}
→ { "execution_payload": {…}, "block_hash": "0x…", "block_value": "0x…" }
```

Under the hood it calls `engine.Client.RequestPayloadBuild` → sleep →
`engine.Client.GetPayloadRaw` → `builder.ModifyPayloadExtraData`. Same code
paths as a real builder; just driven on demand rather than off CL events.

## Known limitations

- **Reth ≤ 2.2 doesn't support Amsterdam.** The harness will refuse the
  `engine_getPayloadV6` call. Switching is a one-liner in `el.py` once
  reth ships Amsterdam.
- **Per-fixture cost ≈ 10–15 s**, dominated by Docker container boot. For a
  large corpus this is fine; for tight iteration on a single fixture it's
  worth setting `--work-dir` to a stable path so the warm-cached image
  startup is reused.
- **No parallel mode yet.** A future iteration could pool ELs or run several
  fixtures in parallel against separate containers.

## License

MIT — see [LICENSE](LICENSE).
