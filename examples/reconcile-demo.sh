#!/usr/bin/env bash
# Reconcile a customer's Fusion export with an ODM's Agile export of the same product.
#
#   examples/reconcile-demo.sh FUSION.xlsx AGILE.xlsx
#
# Two companies, two terminals: each has its identity set once (here via
# OPENBOM_ORG, since this local simulation shares one database). Uses a
# throwaway database (override with OPENBOM_HOME) and the openbom on PATH.
set -euo pipefail
FUSION=$1
AGILE=$2
export OPENBOM_HOME=${OPENBOM_HOME:-$(mktemp -d)/openbom}
here=$(cd "$(dirname "$0")" && pwd)

customer() { printf '\n\033[1m[customer] $ openbom %s\033[0m\n' "$*"; OPENBOM_ORG=customer OPENBOM_USER=dana openbom "$@"; }
odm()      { printf '\n\033[1m[odm]      $ openbom %s\033[0m\n' "$*"; OPENBOM_ORG=odm OPENBOM_USER=lin openbom "$@"; }

openbom init >/dev/null
openbom org create customer --name "Customer" >/dev/null
openbom org create odm --name "ODM manufacturing site" >/dev/null

# Each company imports its own PLM export into its own workspace (profiles are auto-detected).
customer import "$FUSION" --new-ws cust-tray --apply >/dev/null
customer commit -m "Fusion EBOM"
odm import "$AGILE" --new-ws odm-tray --apply >/dev/null
odm commit -m "Agile MBOM"

# The customer runs the reconciliation: it pins both sides and proposes a correspondence.
customer reconcile init tray-recon cust-tray odm-tray
customer reconcile status
customer reconcile pair customer:09-957220 odm:MTTEMP-S927101354
customer reconcile accept customer:05-900012 --note "coolant is customer-consigned"
customer reconcile commit -m "DVT reconciliation"

# The ODM ships its next export; the customer's reconciliation is now outdated.
python "$here/make_agile_rev2.py" "$AGILE" "$OPENBOM_HOME/agile-rev2.xlsx" >/dev/null
odm import "$OPENBOM_HOME/agile-rev2.xlsx" -w odm-tray --apply >/dev/null
odm commit -m "ECO: SSD qty, HPM qty, cable MPN"
customer outdated
customer update --all
customer reconcile commit -m "Re-reconcile after ODM ECO"
customer reconcile diff
customer verify --reveal
echo "database: $OPENBOM_HOME"
