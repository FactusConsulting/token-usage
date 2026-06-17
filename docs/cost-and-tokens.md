# Cost & token accounting in Langfuse

How `token-usage` maps ccusage data into Langfuse, how cost is calculated, and
the operational details that aren't obvious from the code.

## Data model: ccusage → Langfuse

For each `(source, day)` the shim emits:

- **one trace** — id `ccusage-<host>-<source>-<date>`, name `ccusage:<source>`,
  tags `[<host>, <source>]`.
- **one generation per model** — id `<trace-id>-gen-<model>`, carrying that
  model's token counts in Langfuse v3 **`usageDetails`**.

`<host>` is `TOKEN_USAGE_HOSTNAME` (or `socket.gethostname()`), `<source>` is a
ccusage source (claude, codex, …), `<model>` is the real model name.

Because the trace and generation ids are **deterministic** (no timestamp in the
id), Langfuse **upserts** on re-send. Running an import repeatedly — or the
hourly timer overlapping the same days — never creates duplicates; it just
refreshes the same date-keyed records. The only thing that changes an id is the
model label, so changing how a model is named (see codex below) leaves the old
generations orphaned until the trace is re-created.

## Cost is computed by Langfuse, not ccusage

The shim sends **only token counts** (`usageDetails`) and deliberately does
**not** send `costDetails`. Langfuse computes cost itself = Σ over each token
type of `tokens × that type's price`, using its own model-price table.

Why let Langfuse price instead of forwarding ccusage's cost:

- **Historical pricing.** Langfuse model definitions carry effective-dated
  prices and pick the price valid at the observation's timestamp. ccusage ships
  a single current price snapshot and applies it to every date.
- **Self-hosted models stay free.** A model with no price definition (e.g. the
  self-hosted `[pi] gemma…`) computes to `0`, which is correct — there is no
  per-token cost. Forwarding a bogus number would be worse.
- **Newest models are covered.** A current Langfuse already knows the 2026
  Claude 4-series (opus-4-8, opus-4-7, sonnet-4-6, haiku-4-5, fable-5) *and*
  gpt-5.5, including cache rates.

Trade-off to be aware of: a price that is **missing at ingestion** yields `0`,
and adding it later does **not** retroactively recompute stored observations —
re-ship those days (idempotent) to apply the new price. The rolling hourly
window self-heals recent days.

## Token caching is input-side

Prompt caching only ever applies to **input**; output is never cached. A Claude
request splits input into three buckets, each with its own price — illustrated
with the `claude-opus-4-8` rates:

| usageDetails key                | what it is              | price/M  | vs input |
| ------------------------------- | ----------------------- | -------- | -------- |
| `input`                         | uncached input          | $5.00    | —        |
| `cache_creation_input_tokens`   | input written to cache  | $6.25    | 1.25×    |
| `cache_read_input_tokens`       | input read from cache   | $0.50    | 0.1×     |
| `output`                        | generated output        | $25.00   | —        |

Total input = `input + cache_creation + cache_read`. Cache writes carry a
premium; cache reads are ~90% cheaper than fresh input — that's the whole point
of prompt caching.

This is why the dashboard numbers look the way they do: a model can show
**billions** of tokens but a modest cost, because the bulk is cache-reads at
$0.50/M, not $5/M. Before this was wired up, cache tokens lived in trace
`metadata` where Langfuse ignored them, so token totals undercounted by ~100×
and costs were wrong.

> Caveat: ccusage reports a single `cacheCreationTokens` figure and does not
> split 5-minute vs 1-hour cache writes. Langfuse prices them at the 5-minute
> rate; 1-hour cache writes (pricier) are slightly undercounted. Cache *reads*
> dominate, so the effect is minimal.

## Per-source model naming (e.g. codex)

ccusage doesn't return `modelBreakdowns` for every source. claude does; codex
instead names its model in a `models` dict (e.g. `{"gpt-5.5": {...}}`). The shim
uses that real model name so Langfuse can price it — a generic label like
`codex` or `aggregate` matches **no** model definition and shows cost `0`. The
source is still preserved (trace name `ccusage:codex`, `codex` tag), so you can
filter by source while the model is priced correctly as `gpt-5.5`.

## Multiple machines

Each machine should set a distinct `TOKEN_USAGE_HOSTNAME` (e.g.
`lwa002-windows`, `lwa002-wsl2`). The hostname is the trace's host tag, so in the
**Traces** view you can `Filters → Tags → <host>` to scope to one machine, or
group a custom dashboard by host. Windows (Claude Code / codex) and WSL2 keep
separate logs, so their data is genuinely distinct, not duplicated — provided
WSL2's ccusage reads its own `~/.claude`, not the Windows logs via `/mnt/c`
(check with `ccusage claude daily --json --since <date>`).

## Dashboard tips

- **Usage by type** (Model Usage panel) splits tokens into input / output /
  cache_creation / cache_read. **Usage by model** sums them per model.
- **Cost by model / Cost by type** show the Langfuse-computed USD.
- The **Model costs** table lists tokens and USD side by side.
- Note: on a Claude subscription (Max/Pro) the USD is the *API-equivalent* value
  ccusage's tokens imply, not your flat subscription bill.

## Backfilling history

`CCUSAGE_SINCE_DAYS` controls how many days back a run ships (default 7 for the
hourly timer). For a one-off backfill set it on the command line, e.g.
`CCUSAGE_SINCE_DAYS=300 token-usage`. ccusage only has whatever history is in the
machine's local logs, so a wide window just ships everything available; missing
days simply ship `0`. Re-running is safe (idempotent, see above).
