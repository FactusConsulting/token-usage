# Cost & token accounting in Langfuse

How `token-usage` maps ccusage data into Langfuse, how cost is calculated, and
the operational details that aren't obvious from the code.

## Accounting & billing model (the one principle)

The design is deliberately thin: **each machine ships only raw token counts;
Langfuse owns all cost math.** The shim never sends a dollar figure
(`costDetails` is omitted) — it forwards `usageDetails` (input / output /
cache-creation / cache-read tokens, per model) and lets Langfuse multiply by its
own dated model-price table.

What that means for the numbers on the dashboard:

- The USD is **Langfuse's** calculation from published list prices (e.g.
  `claude-opus-4-8` at $5 / $25 per M input/output + standard cache
  multipliers), applied at each session's own date.
- It is an **API-equivalent estimate, not a bill.** Claude Code / Codex run on
  flat subscriptions, so the figure answers "what would these tokens cost at API
  list prices" — useful for comparing machines / projects / models, not for
  reconciling an invoice.
- ccusage also computes a cost, ~9% higher (it adds long-context premium tiers).
  We intentionally **ignore ccusage's number** and trust Langfuse's table, so
  every machine prices identically and historically. Details below.

## Data model: ccusage → Langfuse

By default the shim ships at **session** granularity. For each ccusage session
it emits:

- **one trace** — its OTel trace id is derived from the key
  `ccusage-<host>-<source>-sess-<sessionId>`, and it is mapped onto
  Langfuse's first-class fields: `userId` = `<host>`, `sessionId` =
  `<sessionId>`, name = `ccusage:<source>`. The only tag is
  `project:<basename>` — a machine-independent label, so the same project
  unifies across machines; the full path stays in `metadata`.
- **generations per model**, carrying that model's token counts in
  **`usageDetails`**. On Langfuse v4 user, session, trace name, tags and
  metadata are set on every generation, not only on the trace: v4 queries
  observations directly.

`<host>` is `TOKEN_USAGE_HOSTNAME` (or `socket.gethostname()`), `<source>` is a
ccusage source (claude, codex, …), `<model>` is the real model name. Set
`CCUSAGE_GRANULARITY=daily` for one trace per calendar day instead
(`ccusage-<host>-<source>-<date>`, no sessionId/project).

### Nothing is ever sent twice

Langfuse v4 stores observations append-only. A record re-sent with the same id
is not an update but a second row, and every sum counts both — so the v3-era
upsert (re-sending each session's running total every hour) would double the
numbers. Each run instead asks Langfuse what it holds for this host
(Observations API v2), compares that with what ccusage reports now, and ships
only the difference per session and model as one more generation in the same
trace, dated from where the last one ended. A session used over three days is
therefore several generations whose tokens add up to ccusage's totals.

Langfuse is the only state. A wider window, a reinstall, or a second machine
backfilling never duplicates: whatever Langfuse already holds is subtracted
first. A per-user lock keeps two runs on one machine from shipping the same gap.

What cannot be expressed as an addition — Langfuse holding more than ccusage
reports, or data shipped with a wrong usage key or price — needs `--reship`: it
deletes the traces of every session in the window that ccusage still has, waits
until Langfuse confirms the deletion, and ships those sessions in full.
Sessions no longer in the local logs are never deleted.

### Traces from the pre-v4 shim

Earlier versions wrote through the legacy ingestion API, under readable trace
ids (`ccusage-…`). Langfuse's dual write copies those into the v4 tables
incompletely (most rows lose their user, session and name), so the comparison
ignores them. Instead, every run first moves its host's legacy traces over:
reads them from the v3 API, ships what the v4 data does not hold yet as OTel
generations, waits until Langfuse returns them, then deletes the legacy traces.
This needs the v3 API, which exists only while the server runs the `dual` (or
`legacy`) write mode — so every machine must have run this version once before
the server is switched to `events_only`. A machine that no longer runs
token-usage is moved with `token-usage --migrate-host <host>` from any machine.

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
re-ship those sessions with `token-usage --reship --since …` to apply the new
price.

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

> **The cache-read key is provider-specific.** Langfuse's built-in price table
> prices Anthropic models under both `cache_read_input_tokens` and
> `input_cache_read`, but OpenAI models under `input_cache_read` /
> `input_cached_tokens` only. A key Langfuse does not price is worth **$0** —
> the tokens still appear in the dashboard, and nothing errors. Sending
> Anthropic's name for every source therefore left cache reads unpriced on the
> codex models, where they are the overwhelming majority of the volume. The
> shim now picks the key by model family (`_cache_read_key`), emitting exactly
> one so the token totals stay correct.

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
source is still preserved as the trace name (`ccusage:codex`), so you can filter
by source while the model is priced correctly as `gpt-5.5`.

## Multiple machines

Each machine should set a distinct `TOKEN_USAGE_HOSTNAME` (e.g.
`lwa002-windows`, `lwa002-wsl2`). The hostname becomes the trace's **`userId`**,
so the **Users** view breaks spend down per machine and any panel can group by
user. Windows (Claude Code / codex) and WSL2 keep separate logs, so their data
is genuinely distinct, not duplicated — provided WSL2's ccusage reads its own
`~/.claude`, not the Windows logs via `/mnt/c` (check with
`ccusage claude session --json --since <date>`).

## Dashboard tips

- **Usage by type** (Model Usage panel) splits tokens into input / output /
  cache_creation / cache_read. **Usage by model** sums them per model.
- **Cost by model / Cost by type** show the Langfuse-computed USD.
- The **Model costs** table lists tokens and USD side by side.
- Note: on a Claude subscription (Max/Pro) the USD is the *API-equivalent* value
  ccusage's tokens imply, not your flat subscription bill.

## Backfilling history

`CCUSAGE_SINCE_DAYS` controls how many days back each run ships (default 14).
For a one-off backfill use the flag: `token-usage --since-days 300` (or
`--since 2026-01-01`). ccusage only has whatever history is in the machine's
local logs, so a wide window just ships everything available that Langfuse
does not hold yet. Re-running is safe: only the difference is ever shipped (see
above).
