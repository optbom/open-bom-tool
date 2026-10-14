"""BOM ingestion: turn an exported BOM file into parts and a workspace draft.

Pipeline: read (CSV/XLSX) -> map columns -> normalize rows -> plan (resolve
mappings, rebuild the tree from levels, reconcile with existing parts) ->
apply (write parts and replace the draft).

Import never commits. The committed change record stays the source of truth;
import is just a fast way to produce a draft you then review and commit.

v1 reads one file shape: an indented BOM (a level column), the common export
format of Agile, Arena, Windchill, and ODM spreadsheets.
"""
