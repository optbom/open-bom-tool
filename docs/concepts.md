# Concepts

A short glossary. The [README](../README.md) shows them in use.

## Structure

| Term | Meaning |
|---|---|
| **Part** | A stable identity, written `org:part_id` (e.g. `acme:SRV-1U-001`). Owned by the org that authored the record, which isn't necessarily the manufacturer. |
| **Node** | One revision of a part: an immutable, hashed record (revision, name, manufacturer, MPN, approved manufacturer list, cross-references). Revising a part creates a new node. |
| **Configuration** | An assembly's structure at one point in time: a node plus its child entries. Hashed, so any change below it changes its hash and every ancestor's. |
| **Entry** | One line in a configuration: child node, quantity, unit, reference designators, and optional substitutes. The parent owns its entries. |
| **Substitute** | Another part that can fill the same line (line-level alternate). Never adds quantity. |
| **AML** | Approved manufacturer list: the manufacturer part numbers approved for one part. Part of the node's hash. |
| **Cross-reference** | Another party's part number for the same item (`customer`, `supplier` identifiers). Used for matching. |

## History

| Term | Meaning |
|---|---|
| **Workspace** | One product root's history, owned by one org. |
| **Draft** | A workspace's mutable working state: the edges you edit or import. |
| **Commit** | An immutable snapshot of the draft: the root configuration, parent commit, author, and message. Commits store snapshots; diffs are computed. |
| **External entry** | A reference to another workspace's configuration, **pinned** by hash. It doesn't change when the source changes; `outdated` and `update` move it deliberately. |
| **Opaque** | External configurations aren't expanded in views by default; `--reveal` simulates the supplier disclosing them. |
| **Salt** | Configurations are salted per workspace, so their hashes can't be brute-forced from public parts. |
| **Identity** | The org an installation acts for, configured once (`openbom config`) like `git config user.name`. You can change only what your org owns. |

## Ingestion

| Term | Meaning |
|---|---|
| **Profile** | Reads one source layout (an Agile export, a Fusion report, a generic indented BOM) into a common intermediate form. A PLM API connector would be another profile. |
| **Import plan** | A dry run: what an import would add, revise, or change against HEAD, plus issues to resolve. `--apply` writes the draft; it never commits. |
| **Mapping** | A remembered decision about a part number in imported files: rename it, treat it as another workspace's assembly, or ignore it. |

## Reconciliation

| Term | Meaning |
|---|---|
| **Reconciliation** | A workspace, owned by whoever runs it, that pins two other workspaces describing the same product. |
| **Evidence** | Why two parts were paired: explicit cross-references (trusted), or a shared manufacturer part number (paired only when unambiguous). |
| **Correspondence** | The committed agreement: both sides' pinned hashes, the part pairs, and accepted differences. |
| **Finding** | A classified difference, computed from the correspondence: *match*, *regrouped*, *moved*, *quantity*, *identity*, *sourcing*, or *only in* one side. |
| **Approver** | The side whose approved manufacturer lists the sourcing check uses. Defaults to the runner's own side. |

## Deferred work

Code comments refer to deferred items by number. Each has a seam in the design so it can be added later.

| # | Item | | # | Item |
|---|---|---|---|---|
| D1 | Physical/serialized instances (as-built) | | D11 | Users, authentication, SSO |
| D2 | BOM standards import/export (CycloneDX, CISA HBOM, IPC) | | D12 | Access control and grants |
| D3 | Identifier resolution (GS1 GTIN, JEDEC JEP106) | | D13 | Federation between installations |
| D4 | Rich edge types (supersedes, built-from), effectivity | | D14 | HTTP API and web UI |
| D5 | Rich edge attributes | | D15 | PLM integrations |
| D6 | Branches and tags | | D16 | Disclosure bundles for offline verification |
| D7 | Merges | | D17 | Scale and performance |
| D8 | ECO/ECN semantics and approvals | | D18 | Hash algorithm agility |
| D9 | Events, attestations, provenance | | D19 | Opt-in automatic cross-org updates |
| D10 | Signatures and trusted timestamps | | D20 | Separating identity fields from descriptive metadata |
