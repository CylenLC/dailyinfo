# Canonical Publication Architecture (Phase 2A)

DailyInfo now has a delivery-independent publication boundary:

```text
pipeline result
    -> StructuredPublicationAdapter
    -> PublicationFinalizer
    -> validation + relationship linking
    -> PublicationBundle
    -> PublicationStore
    -> future delivery publishers
```

The existing `briefings/` and `pushed/` directories remain unchanged. The
canonical layer is additive and does not send Discord messages, write
`dailyinfo-web`, perform Git operations, or track delivery state.

## Contract v1

`SCHEMA_VERSION` is `1`. The only canonical categories are:

```text
papers, ai_news, code, resource, arxiv
```

Unknown schema versions and categories fail closed.

### Item

An `Item` contains `id`, `category`, title, nested source metadata, authors,
source publication time, retrieval/publication lifecycle times, summary,
optional significance note, tags, language, and `briefing_ids`.

Item identity is resolved in this order:

1. an explicitly supplied canonical id;
2. an arXiv stable id, when available;
3. a SHA-256 digest of a stable external id (for example GitHub owner/repo or
   a feed GUID), using the canonical source namespace in the hash material;
4. a SHA-256 digest of the canonical public item URL, using the canonical
   source namespace in the hash material.

Titles, summaries, timestamps, random UUIDs, and file paths never create an
identity. The URL fallback removes fragments and common analytics query
parameters, but retains other query parameters. The finalizer stores this
canonical public URL in source metadata as well, so tracking-only URL changes
do not create false publication updates.

For source-derived identities, the identity material is
`external:{source_namespace}:{external_id}`. The namespace is a stable,
lowercase machine key derived from the configured source key; separators are
removed, and arXiv aliases use the `arxiv` namespace. Therefore display-name
variants such as `OpenReview` and `Open Review` do not create separate
namespaces, while two different source namespaces with the same external ID
cannot collide. Explicit IDs are still syntax-validated and remain subject to
Store collision and category checks.

`id` and `category` are immutable identity fields. Store writes reject an item
with an existing id in another category as an identity migration. Other item
fields can update when their semantic content changes.

### Briefing

The briefing id is always `{category}-{YYYY-MM-DD}`. The pair `(category,
date)` therefore has one canonical briefing. Re-finalization updates the
existing record; it cannot create a `-v2` record.

Briefing `body` is the complete existing DailyInfo Markdown. It is stored as
content, not parsed back into structured items. The ordered `item_ids` list is
the editorial order; authors preserve source order, tags are de-duplicated and
sorted, and the bundle's `items` list is serialized in id order for
determinism.

All internal datetimes are timezone-aware UTC and serialize as ISO-8601 with a
`Z` suffix. Date-only source values are interpreted in the configured business
timezone (`Asia/Shanghai` by default) and then normalized to UTC. A briefing
date is a calendar date and is not inferred from a host-local `datetime.now()`.

## Publication Field Semantics

`schema_version` is contract metadata and is fixed at `1`. The remaining Item
fields have these frozen meanings:

| Field | Class | Mutable | Semantic hash | Persistence |
| --- | --- | --- | --- | --- |
| `id`, `category` | Identity | No | Yes | Always; category migration rejected |
| `title`, `source`, `source_published_at`, `authors`, `summary`, `why_it_matters`, `tags`, `language` | Semantic content | Yes | Yes | On content change |
| `briefing_ids` | Relationship | Yes; set-like | No | On membership change; sorted deterministically |
| `retrieved_at` | Lifecycle metadata | Yes; latest retrieval | No | Persist latest supplied value |
| `published_at` | Lifecycle metadata | Restricted | No | Set on create; preserve first stored value |
| `updated_at` | Lifecycle metadata | Yes; caller-supplied record update time | No | Persist supplied value; preserve existing value when omitted |

For Briefing, `id` and `category` are identity fields; `item_ids` is ordered
editorial relationship/composition metadata and is included in the
Briefing/Bundle semantic hash; `generated_at`, `published_at`, and
`updated_at` are lifecycle metadata. Authors preserve source order, tags are
de-duplicated and sorted, and Bundle items are serialized by Item ID.

Lifecycle policy is explicit: an existing Item receives the latest incoming
`retrieved_at`; its first `published_at` is retained. A new `updated_at` is
persisted when supplied, while an omitted value preserves the previous one.
No clock is consulted by the Finalizer.

## Finalizer and current pipeline gap

`StructuredPublicationAdapter` accepts the current `datasource.Item` shape or
a mapping, but reads item summaries and other canonical fields only from
explicit structured fields. It intentionally ignores `content` as a summary
and never parses final Markdown.

The current five pipelines retain title/date/url/content/extra and produce a
combined Markdown briefing. They do not yet retain per-item LLM summary,
why-it-matters, tags, authors, content language, or explicit retrieval and
publication timestamps. Consequently, the current `dailyinfo run` is not
automatically wired to finalization: doing so would require fabricating fields
or reverse-parsing Markdown. A future pipeline integration point is directly
after the structured item list and successful Markdown generation, with an
adapter populated by structured LLM output.

Current source-shape audit:

| Category | Pipeline/source shape retained before finalization | Current gap |
| --- | --- | --- |
| `papers` | RSS/scrape/API items expose title, date, article URL, and source-specific `extra` | no structured summary, significance, authors, tags, language, or lifecycle timestamps |
| `ai_news` | RSS items expose title, date, URL, and article body for `use_content` sources | body is source content, not an LLM summary; no structured canonical fields |
| `code` | GitHub/HuggingFace items expose title/description, date, URL, and code metrics in `extra` | programming-language metadata is not content language; no structured summary or lifecycle timestamps |
| `resource` | DLUT HTML/API items expose title, parsed date, URL, and occasional source-specific metadata | no structured summary, significance, authors, tags, language, or lifecycle timestamps |
| `arxiv` | RSS items expose title, date, and URL | FreshRSS query does not retain the feed GUID/arXiv id; URL-derived identity is used until that metadata is retained |

The adapter therefore requires real structured values for required publication
fields. It does not silently use a feed URL, article body, title, or Markdown as
a substitute for missing canonical data.

## Validation and integrity

Validation covers schema, category, stable id syntax, required text, public
HTTP(S) source URLs, timezone-aware timestamps, unique ids, and the complete
briefing/item relationship. Source URLs reject credentials, localhost, local
hostnames, private/internal IPs, and non-HTTP schemes. Public fields reject
obvious authorization headers, bearer tokens, webhook URLs, key-shaped
secrets, stack traces, local absolute paths, and localhost references.

For a bundle:

```text
briefing.item_ids contains item.id
item.briefing_ids contains briefing.id
briefing.category == item.category
```

The store additionally checks all stored records, so a dangling reverse link,
missing object, duplicate identity, or category mismatch makes readback fail
closed.

## Hash semantics and idempotency

`item_content_hash`, `briefing_content_hash`, and `bundle_content_hash` are
SHA-256 over canonical UTF-8 JSON (`sort_keys=True`, compact separators,
`ensure_ascii=False`). Item ordering in a bundle is canonicalized by id, while
briefing item order remains significant.

Semantic hashes include identity/category, source metadata, source publication
time, content fields, and briefing composition/body. Item `retrieved_at`, Item
`published_at`, Item `updated_at`, Briefing `generated_at`/`published_at`/
`updated_at`, and Item relationship membership are excluded as
runtime/record metadata. This means a repeated fetch or re-finalize of
unchanged content does not change the semantic hash merely because lifecycle
timestamps changed. Lifecycle changes can still cause a Store write when the
complete persisted representation changes.

Store actions are:

```text
identity absent + valid bundle       -> create
identity present + complete record same -> no-op
identity present + semantic/relationship/lifecycle change -> update
same Item id + different category     -> reject identity migration
```

Therefore a same-hash relationship or lifecycle change is not swallowed by a
hash-only no-op check.

## Store and finalized state

The default root is `WORKSPACE_ROOT/publications`, where `WORKSPACE_ROOT` is
the same environment-aware data root used by the existing scripts. The current
layout is:

```text
publications/
├── items/{category}/{quoted-item-id}.json
└── briefings/{YYYY}/{MM}/{DD}/{category}/
    ├── briefing.json
    └── briefing.md
```

This filesystem layout is not the semantic contract and can later be replaced
by SQLite, object storage, or a Git-backed store without changing identities.
Each JSON and Markdown file is written through a same-directory temporary file,
flush/fsync, and atomic replace. Readback validates JSON and cross-object
integrity; there is no manifest because the canonical briefing and item files
already contain the required identity and relationship metadata without a
second copy that could drift.

An object is `FINALIZED` only after construction, full validation, and complete
store persistence succeed. Discord/Web success is not part of this state.
Delivery state belongs to Phase 2B/2C and is deliberately absent from the
models.

Historical `briefings/` and `pushed/` files are not backfilled in Phase 2A.
Backfill should be a separate, explicitly validated migration because old
Markdown lacks enough structured item metadata for safe reconstruction.

`conference` and `social` are existing legacy runtime categories but are
explicitly outside Publication Contract v1. They are not implicitly mapped to
`papers` or `ai_news`, and must bypass this layer until a future contract
revision adds them.
