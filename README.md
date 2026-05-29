# buildoor-tester

Turn [execution-spec](https://github.com/ethereum/execution-specs) tests into
real, signed Ethereum transactions — including the setup they need (faucet
funding, the deterministic-deployment factory, contract deploys) — so a block
builder like [buildoor](https://github.com/ethpandaops/buildoor) can put them
in a block on a devnet.

Three commands:

- **`eest-replay submit`** — the primary tool: submit a spec test's
  transactions directly to a live devnet/testnet RPC. The network's own
  consensus + builder (e.g. buildoor) produce the blocks; `execute` funds the
  sender, deploys the test's contracts, and broadcasts the test transactions
  into the mempool. Works against a long-living testnet or a local kurtosis
  devnet alike — no local node needed. Optional `--csv` records what was sent.
- **`eest-replay export`** — for when there is **no** running network: boot a
  throwaway geth, materialize the same transaction sequence locally, and write
  it to a CSV plus a matching `genesis.json` a consumer can boot from.
- **`eest-replay run`** — the correctness oracle: replay a *filled* fixture
  against a throwaway geth and diff the produced block (including the
  EIP-7928 Block Access List) field-by-field against the fixture's expected
  block. Optionally route the build through buildoor.

## `submit`: run a spec test against a devnet/testnet

```
eest-replay submit <test-selector> --fork <Fork> --rpc <url> --chain-id <N> \
    --seed-key <funded-key> [-k <filter>] [--csv out.csv]
```

This is the simplest and most direct path. On a real network blocks are
already produced, so there is **no local EL and nothing in the middle** — the
tool points EEST `execute remote` straight at `--rpc`:

```
  eest-replay submit ──► execute remote ──► eth_sendRawTransaction ─┐
                                                                    ▼
                                          devnet / testnet mempool
                                                                    │
                                  validators + builder (buildoor) build blocks
                                                                    │
                                          txs included on-chain  ◄──┘
```

`execute` reads the faucet's live nonce, deploys the deterministic CREATE2
factory if it isn't already present, deploys the test's contracts, funds the
test EOAs, and submits the test transactions — all into the target network's
mempool, where buildoor picks them up.

### Examples

```bash
# Against a local kurtosis devnet (buildoor is the builder there)
eest-replay submit \
  'tests/frontier/opcodes/test_push.py::test_push[fork_Prague-state_test-PUSH1]' \
  --fork Prague --rpc http://127.0.0.1:8545 --chain-id 7928 \
  --seed-key 0x<funded-devnet-key>

# A Block Access List test (Amsterdam), recording what was sent
eest-replay submit \
  tests/amsterdam/eip7928_block_level_access_lists/test_block_access_lists.py \
  -k test_bal_nonce_changes \
  --fork Amsterdam --rpc http://127.0.0.1:8545 --chain-id 7928 \
  --seed-key 0x<funded-devnet-key> --csv ./submitted.csv
```

### Requirements & notes

- `--seed-key` must be **funded on the target network**. The default is only
  prefunded on local devnets — pass your own for a real testnet.
- `--fork` must match the network's active fork; `--chain-id` is cross-checked
  against the RPC and the run aborts on mismatch.
- The deterministic factory is usually already deployed on a real testnet
  (execute skips it). On a fresh devnet execute deploys it via Nick's keyless
  method, which needs the EL to accept unprotected txs.
- We only care that the transactions are **submitted and included**; `execute`
  additionally verifies post-state and will report failure on a mismatch even
  though the txs were still broadcast. With `--csv` the recorded rows are the
  ground truth of what went on-chain.
- On a shared network, override `--eoa-start` per caller so ephemeral EOA keys
  don't collide with another run.
- **Gas prices:** if the target RPC reports a zero priority fee (common on
  testnets), `execute` derives a max-fee of 0 and every tx is rejected below
  base fee. Pass explicit WEI values, e.g.
  `--max-fee-per-gas 5000000000 --max-priority-fee-per-gas 1000000000`
  (5 / 1 gwei) — comfortably above a testnet base fee.
- **Hosted endpoints:** `--rpc` may embed `user:pass@` basic-auth creds; the
  CSV-recording proxy applies them and sends a normal User-Agent (some
  gateways 403 the default urllib agent). Note that some read/load RPC
  gateways reject `eth_sendRawTransaction` outright (e.g. a canned
  "gas limit is too high" even for a 21000-gas tx) — point at a writable RPC.

## `export`: materialize transactions locally (no network)

Use this when you don't have a devnet running and just want the transaction
sequence on disk. It boots a throwaway geth, drives block production itself via
the Engine API, and writes the captured transactions plus a matching
`genesis.json` — so a consumer can later boot a compatible EL and replay them.
(If you already have a devnet, prefer `submit`.)

```
eest-replay export <test-selector> --fork <Fork> [-k <filter>] [--output dir]
```

```
              ┌──────────────── eest-replay export ─────────────────┐
              │  boot throwaway geth from a devnet genesis           │
              │   (prefunds the EEST seed account)                  │
              │  start a recording proxy in front of geth's RPC      │
              │  run EEST `execute remote` against the proxy:        │
              │    • fund the sender from the seed key               │
              │    • deploy the deterministic CREATE2 factory        │
              │    • deploy the test's contracts                     │
              │    • send the test transactions                      │
              │  capture every eth_sendRawTransaction, in order      │
              │  enrich via eth_getTransactionByHash → write CSV     │
              └──────────────────────────────────────────────────────┘
                       │                         │
                       ▼                         ▼
                 transactions.csv          genesis.json + meta.json
```

`execute` does the hard part (funding math, live contract deployment with real
on-chain addresses, signing). The proxy just tees the raw signed transactions
to a CSV — because geth really executes everything, the captured sequence is
correct and replayable against an EL booted from the emitted `genesis.json`.

### Example

```bash
# A simple opcode test (Prague)
eest-replay export \
  'tests/frontier/opcodes/test_push.py::test_push[fork_Prague-state_test-PUSH1]' \
  --fork Prague --output ./out/push

# A Block Access List test (Amsterdam / EIP-7928)
eest-replay export \
  tests/amsterdam/eip7928_block_level_access_lists/test_block_access_lists.py \
  -k test_bal_nonce_changes --fork Amsterdam --output ./out/bal

# A worst-case benchmark transaction (Prague, 1M gas)
eest-replay export \
  tests/benchmark/compute/instruction/test_arithmetic.py \
  -k "test_arithmetic and opcode_ADD and not ADDMOD" \
  --fork Prague --include-benchmark --gas-benchmark-values 1 --output ./out/add
```

### Output

| file | contents |
|------|----------|
| `transactions.csv` | one row per signed tx in submission order: `seq, block_number, tx_index, tx_hash, type, from, to, nonce, value, gas, gas_price, max_fee_per_gas, input_len, raw` |
| `genesis.json`     | a geth/reth genesis activating all forks up to `--fork`, prefunding the seed account — boot an EL from this to replay the txs |
| `meta.json`        | run parameters (fork, chain id, seed address, eoa-start) and replay notes |

The `raw` column is the complete signed transaction. A consumer replays the
CSV by submitting each `raw` in `seq` order to an EL booted from `genesis.json`
(same chain id, seed prefunded). The leading rows are setup (deterministic
factory, funding, contract deploys); the trailing rows are the test itself.

Example (`test_bal_nonce_changes`, Amsterdam):

```
seq  from                  to                    note
0    seed                  0x3fab18… (deployer)  fund the factory deployer
1    0x3fab18…             (create)              deploy the CREATE2 factory
2    seed                  0xc50d87… (alice)     fund the test sender EOA
3    0xc50d87… (alice)     0x06e405… (bob)       the test transaction
```

### Key flags

| flag | meaning |
|------|---------|
| `--fork` | fork the test targets; sets the devnet genesis (Prague, Amsterdam, …) |
| `-k` | pytest `-k` filter passed through to `execute remote` |
| `--output` | output directory (default `export/`) |
| `--specs-dir` | path to the execution-specs checkout (default `../execution-specs`) |
| `--chain-id` | devnet chain id (default 7928) |
| `--seed-key` | genesis-prefunded private key that funds the test (devnet only) |
| `--include-benchmark` / `--gas-benchmark-values` | enable benchmark tests |

> [!NOTE]
> The seed key is a **devnet test key** that is prefunded in the emitted
> genesis. Never use a real key — these transactions assume a fresh devnet
> starting from `genesis.json`.

## What's in the box

```
buildoor-tester/
├── pyproject.toml             # uv project for the `eest-replay` CLI
├── src/eest_replay/
│   ├── cli.py                 # `submit` + `export` + `run` subcommands
│   ├── export.py              # submit/export: orchestrate EEST `execute remote`
│   ├── rpc_proxy.py           # recording JSON-RPC proxy (tees sendRawTransaction)
│   ├── devnet_genesis.py      # fresh devnet genesis for a target fork (export)
│   ├── fixture.py             # BlockchainEngineFixture loading + discovery
│   ├── chainspec.py           # fixture.pre + genesis → geth genesis.json
│   ├── el.py                  # throwaway geth lifecycle (Docker)
│   ├── buildoor_client.py     # spawns `buildoor simbuild`, POSTs /build
│   ├── runner.py              # replay: bootstrap → build → diff → advance
│   └── report.py              # aggregate batch results to JSON/Markdown
└── patches/
    ├── 01-engine-client-nullable-slotnumber.patch
    ├── cmd_simbuild.go        # new buildoor subcommand (used by `run`)
    └── README.md              # how to apply
```

## `run`: fixture replay (correctness oracle)

Where `export` produces transactions for a live builder, `run` is the
field-by-field correctness check: it replays a *filled* fixture against a
throwaway geth and diffs the produced block — including the BAL — against the
fixture's expected block.

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
