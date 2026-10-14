# openbom

A cross-org **hardware configuration graph** (v1 prototype). PLM manages a product inside one company; `openbom` manages product configuration *as it crosses company boundaries*: a content-addressed, hash-verifiable identity for what a piece of hardware is made of. Two organizations can agree on exactly which version of a sub-assembly is in a product without either side exposing its internal breakdown.

It borrows Git's object model: node objects (part revisions) are blobs, configurations are trees, change records are commits, and cross-workspace references work like submodules pinned by hash.

**Docs:** [Overview](docs/overview.md) · [Concepts](docs/concepts.md) · [Contributing](CONTRIBUTING.md)

> `openbom` is a working name. It lives in one constant (`openbom.APP_NAME`) plus `pyproject.toml`.

## Install

Requires Python ≥ 3.11. You can activate the venv (`source .venv/bin/activate`) or call `.venv/bin/openbom` directly.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest          # acceptance tests (spec §11)
```

**Identity** is set once per installation, like `git config user.name`: `openbom config --org acme --user alice`. `$OPENBOM_ORG` / `$OPENBOM_USER` override it per process; the demos use that so two companies can share one local database.

Data lives in `$OPENBOM_HOME` (default `~/.openbom`): `openbom.db` (SQLite) plus `config.json` (acting org, user, current workspace). Every command accepts `--home <path>`.

## Demo: reconcile two companies' PLMs

A customer's Fusion EBOM and an ODM's Agile MBOM of the same compute tray become two workspaces, each owned by its org. A third workspace, owned by whoever runs the reconciliation, pins both and commits the agreed **correspondence** between them. That correspondence is versioned, diffable, and verifiable like everything else.

```sh
examples/reconcile-demo.sh FUSION.xlsx AGILE.xlsx      # the whole flow below, in a throwaway database
```

Two companies, two terminals. Each terminal's identity is set once, the way git knows who you are. In this local simulation both share one database, so each terminal exports `OPENBOM_ORG`; a real installation would run `openbom config --org … --user …` once.

```sh
# --- customer's terminal ---------------------------------------------------
export OPENBOM_ORG=customer OPENBOM_USER=dana
openbom import fusion.xlsx --new-ws cust-tray --apply
openbom commit -m "Fusion EBOM"

# --- ODM's terminal ----------------------------------------------------------
export OPENBOM_ORG=odm OPENBOM_USER=lin
openbom import agile.xlsx --new-ws odm-tray --apply
openbom commit -m "Agile MBOM"

# --- customer's terminal: reconcile ----------------------------------------------
openbom reconcile init tray-recon cust-tray odm-tray
openbom reconcile status        # pairs, proposals that need a decision, classified differences
openbom reconcile pair customer:09-957220 odm:MTTEMP-S927101354
openbom reconcile accept customer:05-900012 --note "coolant is customer-consigned"
openbom reconcile commit -m "DVT reconciliation"

# later, after the ODM commits a new export
openbom outdated && openbom update --all
openbom reconcile status        # "Changes since the last reconciliation commit"
openbom reconcile commit -m "Re-reconcile after ODM ECO"
openbom reconcile diff
```

**Matching** pairs parts using evidence:
- *Explicit cross-references* (trusted, even one-to-many): the ODM's "customer part number", the customer's vendor/supplier part numbers, identical part numbers, GTINs, and your `pair` decisions.
- *Inferred* evidence: a manufacturer part number shared by the two approved lists. This pairs automatically only when it's unambiguous on both sides; otherwise it becomes a proposal.

Substitutes on one side that cover the same line fold into their primary's pair. Decisions are keyed by part ID, so they carry over to later revisions.

**Differences** are classified by comparing quantities rolled up under the nearest paired ancestor:

| class | meaning |
|---|---|
| match / regrouped | same quantity; directly, or through intermediate assemblies only one side has |
| moved | same total, under different paired parents |
| quantity | different rolled-up quantity |
| identity conflict | one side uses a part where the other lists a different, related part |
| sourcing | one side uses a manufacturer part the approving side hasn't approved |
| only in X | no counterpart (ODM-only lines are usually detail: screws, labels) |

The two sides are symmetric. The only asymmetry is whose approved manufacturer lists count for the sourcing check: by default the side owned by the org running the reconciliation (override with `--approver WS`). `--strip-prefix odm-tray=MT` ignores a site prefix on one side's part numbers. Each commit stores the pairs and accepted differences; findings are recomputed from them.

## Quickstart: import BOM exports

Most structure arrives as files. `import` reads an indented BOM export (CSV or XLSX, with a level column, as exported by Agile, Arena, Windchill, or ODM spreadsheets) and turns it into parts and a workspace draft. It **never commits**: the committed change record stays the source of truth, and import is a fast way to produce a draft you review and commit.

```sh
openbom init
openbom org create acme
openbom org create pwrco

# Supplier publishes its PSU from its PLM export
export OPENBOM_ORG=pwrco OPENBOM_USER=pat      # pwrco's terminal
openbom import examples/psu-bom.csv --new-ws pwrco-psu-ws --apply
openbom commit -m "PSU from Arena export"

# OEM imports its server EBOM. Without --apply this is a dry run: a plan plus issues.
export OPENBOM_ORG=acme OPENBOM_USER=alice     # acme's terminal
openbom import examples/server-bom-revA.csv --new-ws acme-server-ws
#   error  row 16  TIM-01 quantity 'A/R' is not a positive number
#                  -> fix the file, or: openbom map add "TIM-01" --ignore

# Resolve issues once with persisted mappings; later imports reuse them.
openbom map add TIM-01 --ignore
openbom map add PWR-0800 --from pwrco-psu-ws      # our PN for the supplier's PSU -> external entry
openbom import examples/server-bom-revA.csv --new-ws acme-server-ws --apply
openbom commit -m "EBOM rev A"
openbom tree

# Next export: re-import, and the plan shows exactly what changes against HEAD.
openbom import examples/server-bom-revB.csv
#   ~ acme:CPU-X rev A -> B
#   - acme:MB-X1 -> acme:BRKT-DRV  1 ea
#   ~ acme:MB-X1 -> acme:DIMM-32G  quantity 16 -> 32, refdes +DIMM16-DIMM31
openbom import examples/server-bom-revB.csv --apply
openbom commit -m "EBOM rev B"
```

How the importer behaves:

- **Profiles** handle each source's layout and are detected automatically (`--profile` to force one):
  - `agile`: bilingual headers; ALT rows folded into their primary as substitutes; rev and ECO split.
  - `fusion`: one row per approved manufacturer part, grouped back into lines; vendor parts become supplier cross-references.
  - `generic`: everything else.

  All profiles produce the same intermediate `SourceBOM`. A PLM API connector would be one more profile.
- **Columns** (generic profile) are detected from common header names (`Item Number`, `P/N`, `Qty Per`, `Ref Des`, `Mfr Part Number`, …), even below title rows. For anything it misses, use `--column quantity="Count"`. Unrecognized columns are listed and ignored.
- **Values** are normalized: `1.0` becomes `1`, `Each` becomes `ea`, and refdes ranges like `C1-C4` are expanded.
- **The file replaces the draft.** Re-importing the next export swaps the whole draft, and the plan's preview shows the diff against HEAD before anything is written.
- **Part records** are reconciled with existing parts: new, revised (the revision label changed), or unchanged. If fields changed but the revision label didn't, that's an error unless you pass `--update-metadata`, because metadata is part of the hash.
- **Repeats:** a part listed twice under one parent has its quantities summed (with a warning). A sub-assembly expanded in two places must be broken down identically each time.
- **External entries** keep their existing pin on re-import. Moving a pin is `update`'s job.
- **Mappings** (`map add <pn> --part ID | --from WS | --ignore`) belong to the acting org and apply to every later import.

`import --json` emits the full plan for scripting, or for a future review UI.

## Quickstart: by hand

The same model, built command by command. Simulate two orgs in one local database: a supplier (`pwrco`) publishes a PSU, and an OEM (`acme`) builds a server that uses it.

```sh
openbom init
openbom org create acme --name "Acme Compute"
openbom org create pwrco --name "Power Co"

# Supplier: a PSU with an internal breakdown
export OPENBOM_ORG=pwrco OPENBOM_USER=pat      # pwrco's terminal
openbom part add PSU-800 --rev C --name "800W PSU"
openbom part add FAN-40 --rev A --name "40mm fan"
openbom ws create pwrco-psu-ws --root PSU-800
openbom edge add PSU-800 FAN-40 --qty 1
openbom commit -m "PSU initial"

# OEM: a server, two levels deep, plus the supplier's PSU by reference
export OPENBOM_ORG=acme OPENBOM_USER=alice     # acme's terminal
openbom ws list                                   # sees pwrco's workspace
openbom part add SRV-1U-001 --rev B --name "1U compute server"
openbom part add CHS-1U --rev A --name "1U chassis"
openbom part add SCR-M3x6 --rev A --name "M3x6 screw" --manufacturer "Acme Fasteners" --mpn M3-6-SS
openbom ws create acme-server-ws --root SRV-1U-001
openbom edge add SRV-1U-001 CHS-1U --qty 1
openbom edge add CHS-1U SCR-M3x6 --qty 8
openbom edge add-external SRV-1U-001 --from pwrco-psu-ws --qty 2 --refdes PSU1 --refdes PSU2
openbom status
openbom commit -m "initial server"
openbom tree
```

```
acme:SRV-1U-001 rev B  "1U compute server"  [sha256:eb3847217304…]
├── 1 ea  acme:CHS-1U rev A  "1U chassis"  [sha256:7d1726ca53fa…]
│   └── 8 ea  acme:SCR-M3x6 rev A  "M3x6 screw"  (mfr: Acme Fasteners, mpn: M3-6-SS)
└── 2 ea  pwrco:PSU-800 rev C  "800W PSU"  (refdes: PSU1,PSU2)  ⟵ pwrco-psu-ws
          configuration sha256:089dd783b6e4…  [opaque]
```

Within a workspace, changes propagate automatically: revise a leaf, commit, and every ancestor's hash changes while sibling sub-assemblies keep theirs.

```sh
openbom part revise SCR-M3x6 --rev B
openbom commit -m "screw rev B"
openbom log
openbom diff                                      # HEAD~1 vs HEAD: one revised part
```

Across workspaces, references stay pinned. A supplier commit never silently changes your product; you pull it in explicitly:

```sh
export OPENBOM_ORG=pwrco OPENBOM_USER=pat      # pwrco's terminal
openbom part revise FAN-40 --rev B
openbom commit -m "fan rev B"

export OPENBOM_ORG=acme OPENBOM_USER=alice     # acme's terminal
openbom outdated                                  # PSU has a newer commit
openbom update --all                              # re-pin in the draft
openbom commit -m "pick up new PSU"
openbom diff                                      # "sub-assembly changed (contents not visible)"
openbom tree --reveal                             # simulate the supplier disclosing internals
openbom verify --reveal                           # rehash + schema-check everything reachable
```

Ownership is enforced: acting as `acme`, `openbom part revise pwrco:FAN-40 --rev Z` fails with *part pwrco:FAN-40 is owned by org 'pwrco'; you are acting as 'acme'*.

## Commands

```
init | org create/list | config [--org ORG] [--user NAME] | whoami
part add/revise/show/list
ws create/list/use
import FILE [-w WS | --new-ws WS] [--profile P] [--sheet S] [--column field=Header] [--update-metadata] [--apply] [--json]
reconcile init WS SIDE_A SIDE_B [--approver WS] [--strip-prefix WS=PREFIX]
reconcile status [--all] [--json] | pair PART PART | reject PART PART | accept PART --note N | commit -m | diff [A] [B]
map add <pn> (--part ID | --from WS | --ignore) | list | rm
edge add | add-external | set | rm | list
status | commit -m | log | show | cat-object
tree [REV] [--depth N] [--hashes] [--reveal] [--json]
diff [A] [B] [--reveal] [--json]
outdated | update (<org:part_id> | --all)
verify [REV] [--reveal]
```

Revisions: a full hash, a unique prefix of ≥ 8 hex chars, `HEAD`, or `HEAD~N`. Workspace-scoped commands take `-w/--workspace` and otherwise use the workspace set by `ws use`.

## Architecture

```
src/openbom/
  canonical.py     RFC 8785 JCS, float rejection, sha256: hashing, HMAC
  objects.py       Node / Entry / Configuration / Commit: normalizing constructors + strict parsers
  store.py         SQLite schema; the only module with SQL; Store.get_object() is the single read path
  config.py        home dir, config.json
  identity.py      orgs, simulated acting org, ownership check
  parts.py         part add / revise / history
  workspace.py     workspaces, draft edges, revision refs, commit build (bottom-up, DAG-memoized)
  graph.py         typed loading over an abstract object source, cycle detection, traversal
  diff.py          computed diffs (commits store snapshots, never diffs)
  sync.py          outdated / update for pinned external entries
  status.py        draft vs HEAD
  verify.py        integrity checks over an abstract object source
  ingest/          BOM import: profiles/ (agile, fusion, generic) -> source.py (SourceBOM) -> plan.py (plan/preview/apply)
  reconcile.py     matching, difference classification, correspondence commits
  render/          tree.py (the one v1 BOM representation), refdes.py (range display)
  cli.py           typer wrapper: no SQL, no business logic
```

Key properties:

- **Content addressing.** Every object is stored as its exact canonical bytes; `hash = "sha256:" + sha256(JCS(body))`. Unordered lists are sorted before hashing. Floats are rejected everywhere; quantities are decimal strings.
- **Edges live inside the parent's configuration**, so the parent owns its edges and its hash covers them. Each entry carries the child *node* hash (always visible) and, if the child has structure, its *configuration* hash.
- **Salted configurations.** `salt = HMAC(workspace.secret, JCS(unsalted))`. This stops brute-forcing of opaque hashes while keeping unchanged content's hash stable.
- **Sourcing and substitutes.** A node's `aml` is its approved manufacturer list, and it is hashed: a sourcing change is a controlled change. An entry's `substitutes` are other parts that can fill that line (line-level alternates). They never add quantity.
- **Cross-references.** Node `identifiers` with scheme `customer` or `supplier` record the other party's part number for the same item. Reconciliation matches on them.
- **Opacity is a view rule in v1.** Every org can read every object. `tree`, `diff`, and `verify` don't traverse external configurations unless `--reveal` is passed.
- **The core library has no CLI dependency**, so an HTTP API can wrap it later.

Deferred items (instances, standards-based import/export such as CycloneDX, automatic multi-source merging into one workspace, a manufacturer registry (JEP106) and GTIN resolution, identifier resolution, rich edge types, branches/tags, merges, signatures, users and access control, federation, HTTP API, PLM integrations, disclosure bundles) are out of scope. The seams for them are in place: `kind`, `rel`, the `identifiers`/`attributes` maps, the refs table, the `parents` list, the single `get_object` read path, and object-source-based `verify`. Places where the spec was silent and a simple choice was made are marked `TODO(spec)` in the code.
