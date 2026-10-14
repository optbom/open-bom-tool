# Overview

## The problem

Hardware is built across companies, but its bill of materials has no shared home. A customer's engineering BOM lives in its PLM; the ODM's manufacturing BOM lives in another; the contract manufacturer may have a third. Each describes the same product in its own part numbers, structure, and level of detail and whether two describe the same thing is checked by hand, in spreadsheets and email.

## What openbom is

An open-source tool that gives a product's configuration a shared, verifiable identity across company boundaries, the way Git gives source code one.

- **Content addressed.** Every part revision and every assembly's configuration is hashed, so two companies can agree on which version of a sub-assembly is in a product automatically.
- **Versioned.** Changes are commits: immutable snapshots with history, diffs, and verification.
- **Owned, but referenceable.** Each company owns its parts and workspaces. Others can reference a supplier's assembly, pinned by hash, without seeing inside it, and choose when to take updates.
- **Fed by the systems you already have.** BOM exports (and later PLM APIs) are imported through per-system profiles. Exceptions are resolved once and remembered.
- **Reconciled.** Two companies' BOMs of the same product are matched part by part, their differences classified, and the agreed correspondence committed as its own versioned record.

## How it works, briefly

1. Each org imports its BOM into its own workspace and commits it.
2. A reconciliation workspace pins both sides by hash and proposes how their parts correspond, using cross-references the parties already hold (customer and vendor part numbers, manufacturer part numbers).
3. A person settles what the evidence can't (a pairing, an accepted difference) and commits the agreement.
4. When either side commits a change, the reconciliation shows as outdated, and automated or manual re-running shows exactly what drifted.

## Status

A v1 prototype: a Python library and CLI over a local SQLite database, with several organizations simulated in one database. It is suitable for demos and design feedback, not production use.

**In v1:** the object model and hashing, workspaces and history, cross-org references, BOM import (Agile, Oracle Fusion, and generic CSV/XLSX profiles), and reconciliation.

**Not yet:** user accounts and access control, signatures, a server or web UI, federation between separate installations, PLM API connectors, standards import/export (CycloneDX, IPC), and merging several sources into one workspace. The design leaves room for each; see [concepts](concepts.md#deferred-work).
