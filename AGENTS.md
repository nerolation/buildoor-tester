# AGENTS.md — eest-replay

## What
- Turns EEST (execution-spec) tests → real signed txs → submits to a chain.
- Wraps EEST `execute remote`; records every tx to CSV.
- Goal: tx inclusion, not post-state correctness.

## Commands
- `submit` — primary. test → live devnet/testnet RPC. network builds blocks.
- `export` — test → CSV + genesis, via local geth + Engine API. offline replay.
- `bloat` — submit against a state-actor-bloated local geth `--dev`.
- `recover` — sweep funded EOAs back to seed (uses recovery sidecar).
- `run` — replay prefilled blockchain_test_engine fixtures.

## Flow (submit)
- seed-key funds per-test EOAs (key = eoa-start + i) → deploys CREATE2 factory → deploys test contracts → sends test txs → polls inclusion.
- eoa-start: random per run (avoids nonce reuse).

## Gas
- `--fork` sets tx typing (Prague/Osaka/Amsterdam).
- benchmark default = fork per-tx cap (2**24 = 16,777,216 on Osaka+).
- override: `--transaction-gas-limit <raw>` or `--gas-benchmark-values <whole-millions>`.

## Safeguards (seed ETH)
- `--cleanup` (default on): refund EOAs → net spend ≈ gas.
- `--min-seed-balance <ETH>`: abort if seed too low.
- recovery sidecar always written (records eoa-start).

## Gotchas
- seed-key must be funded on target network.
- low-base-fee devnet: full corpus runs; busy testnet (Sepolia): tight-funded tests fail.
- zero-priority-fee net: pass `--max-fee-per-gas` / `--gas-price` (else maxFee=0 rejected).
- blob (type-3) txs can't be submitted standalone.
- factory deploy = Nick's keyless tx (unprotected, fixed 100k gas); needs allow-unprotected-txs; breaks if fork's creation intrinsic > 100k → pre-deploy factory in genesis.
- BAL (EIP-7928): builder attaches to block; signed tx carries none.
- hosted RPC: embed `user:pass@`; sends Mozilla UA. some read-RPCs reject `eth_sendRawTransaction`.

## Secrets
- never commit keys / RPC URLs / auth. local files only.

## Stack
- Python, `uv`, `click`. EEST via `--specs-dir` (../execution-specs). local EL = geth Docker (`ethpandaops/geth`).
