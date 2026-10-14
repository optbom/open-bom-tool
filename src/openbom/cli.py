"""Command-line interface: a thin typer wrapper over the core library.

No SQL and no business logic here: parse arguments, call the library, format
output. The core library never imports this module (D14).
"""

import functools
import json
from pathlib import Path
from typing import Annotated, Any, Callable, Optional

import typer

from . import APP_NAME, __version__, identity, parts, reconcile, sync, workspace
from .canonical import short_hash
from .config import HOME_ENV, load_config, resolve_home, save_config
from .diff import DiffResult, EdgeInfo, diff_configurations
from .errors import OpenBomError
from .ingest import mappings as ingest_mappings
from .ingest.plan import ERROR, WARNING, ImportPlan, run_import
from .objects import DEFAULT_UOM, decode
from .render import tree as tree_render
from .render.refdes import compact_refdes, refdes_change
from .status import status as compute_status
from .store import Store
from .verify import verify_commit

app = typer.Typer(
    name=APP_NAME,
    help="Cross-org hardware configuration graph (v1 prototype).",
    no_args_is_help=True,
    add_completion=False,
)
org_app = typer.Typer(help="Organizations (simulated in one local database).", no_args_is_help=True)
part_app = typer.Typer(help="Parts and their revisions.", no_args_is_help=True)
ws_app = typer.Typer(help="Workspaces (one product root's history, owned by one org).", no_args_is_help=True)
edge_app = typer.Typer(help="Edit the draft's edges.", no_args_is_help=True)
app.add_typer(org_app, name="org")
app.add_typer(part_app, name="part")
app.add_typer(ws_app, name="ws")
app.add_typer(edge_app, name="edge")
map_app = typer.Typer(help="Import mappings: what a part number in imported files means.", no_args_is_help=True)
app.add_typer(map_app, name="map")
recon_app = typer.Typer(
    help="Reconcile two workspaces' BOMs of the same product into a versioned correspondence.",
    no_args_is_help=True,
)
app.add_typer(recon_app, name="reconcile")

HomeOpt = Annotated[
    Optional[Path],
    typer.Option("--home", help=f"Data directory (default: ${HOME_ENV} or ~/.{APP_NAME}).", show_default=False),
]
WsOpt = Annotated[
    Optional[str], typer.Option("-w", "--workspace", help="Workspace (default: the one set by `ws use`).")
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")]
RevealOpt = Annotated[
    bool, typer.Option("--reveal", help="Also traverse external (opaque) configurations, simulating disclosure.")
]
QtyOpt = Annotated[str, typer.Option("--qty", help="Positive decimal quantity, e.g. 2 or 0.5.")]
UomOpt = Annotated[str, typer.Option("--uom", help="Unit of measure.")]
RefdesOpt = Annotated[Optional[list[str]], typer.Option("--refdes", help="Reference designator (repeatable).")]

_global_home: Optional[Path] = None


@app.callback()
def _main(
    home: HomeOpt = None,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    global _global_home
    _global_home = home
    if version:
        typer.echo(f"{APP_NAME} {__version__}")
        raise typer.Exit()


class Session:
    def __init__(self, home: Optional[Path], *, create: bool = False):
        self.home = resolve_home(home or _global_home)
        self.store = Store.open(self.home, create=create)
        self.config = load_config(self.home)

    @property
    def actor(self) -> identity.Actor:
        return identity.make_actor(self.store, self.config.effective_org, self.config.effective_user)

    def ws(self, ws_id: Optional[str]) -> str:
        ws_id = ws_id or self.config.current_workspace
        if ws_id is None:
            raise OpenBomError("no workspace selected; pass -w <ws_id> or run `ws use <ws_id>`")
        return ws_id

    def save(self) -> None:
        save_config(self.home, self.config)


def command(tapp: typer.Typer, name: str, **kwargs: Any) -> Callable:
    """Register a command that turns library errors into a message + exit 1."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kw: Any) -> Any:
            try:
                return fn(*args, **kw)
            except OpenBomError as exc:
                typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
                raise typer.Exit(1)

        return tapp.command(name, **kwargs)(wrapper)

    return decorator


def echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2))


def h(value: Optional[str]) -> str:
    return short_hash(value) + "…" if value else "-"


def parse_kv(values: Optional[list[str]], flag: str) -> list[tuple[str, str]]:
    pairs = []
    for v in values or []:
        if "=" not in v:
            raise OpenBomError(f"{flag} expects key=value, got {v!r}")
        key, value = v.split("=", 1)
        pairs.append((key, value))
    return pairs


# --------------------------------------------------------------------------
# Setup and identity
# --------------------------------------------------------------------------


@command(app, "init")
def init_cmd(home: HomeOpt = None) -> None:
    """Create the data directory and database."""
    s = Session(home, create=True)
    typer.echo(f"initialized {s.store.path}")


@command(org_app, "create")
def org_create(
    org_id: str,
    name: Annotated[Optional[str], typer.Option("--name", help="Display name.")] = None,
    home: HomeOpt = None,
) -> None:
    """Create an organization."""
    s = Session(home)
    identity.create_org(s.store, org_id, name)
    typer.echo(f"created org {org_id}")


@command(org_app, "list")
def org_list(home: HomeOpt = None) -> None:
    """List organizations (* = acting org)."""
    s = Session(home)
    for org_id, name in identity.list_orgs(s.store):
        mark = "*" if org_id == s.config.effective_org else " "
        typer.echo(f"{mark} {org_id}  {name}")


@command(app, "config")
def config_cmd(
    org: Annotated[Optional[str], typer.Option("--org", help="The org this installation acts for.")] = None,
    user: Annotated[Optional[str], typer.Option("--user", help="Your name, written into commit authors.")] = None,
    home: HomeOpt = None,
) -> None:
    """Set this installation's identity once (like `git config user.name`). With no options, show it."""
    s = Session(home)
    if org is not None:
        identity.require_org(s.store, org)
        s.config.org = org
    if user is not None:
        s.config.user = user or None
    if org is not None or user is not None:
        s.save()
    typer.echo(f"org:  {s.config.org or '-'}")
    typer.echo(f"user: {s.config.user or '-'}")


@command(app, "whoami")
def whoami(home: HomeOpt = None) -> None:
    """Show the identity commands run as, and the current workspace."""
    s = Session(home)
    actor = s.actor
    typer.echo(f"org:       {actor.org}  (from {s.config.identity_source})")
    typer.echo(f"user:      {actor.user or '-'}")
    typer.echo(f"workspace: {s.config.current_workspace or '-'}")


# --------------------------------------------------------------------------
# Parts
# --------------------------------------------------------------------------

DescOpt = Annotated[Optional[str], typer.Option("--description")]
MfrOpt = Annotated[Optional[str], typer.Option("--manufacturer")]
MpnOpt = Annotated[Optional[str], typer.Option("--mpn", help="Manufacturer part number.")]
IdOpt = Annotated[
    Optional[list[str]], typer.Option("--id", help="Identifier scheme=value (gtin, jep106, mpn, other). Repeatable.")
]
UriOpt = Annotated[Optional[list[str]], typer.Option("--uri", help="Drawing/datasheet link. Repeatable.")]
AttrOpt = Annotated[Optional[list[str]], typer.Option("--attr", help="Attribute key=value. Repeatable.")]


@command(part_app, "add")
def part_add(
    part_id: str,
    rev: Annotated[str, typer.Option("--rev", help="Revision label.")],
    name: Annotated[str, typer.Option("--name", help="Part name.")],
    description: DescOpt = None,
    manufacturer: MfrOpt = None,
    mpn: MpnOpt = None,
    ids: IdOpt = None,
    uris: UriOpt = None,
    attrs: AttrOpt = None,
    home: HomeOpt = None,
) -> None:
    """Create a part owned by the acting org."""
    s = Session(home)
    node_hash, node = parts.add_part(
        s.store,
        s.actor,
        part_id,
        revision=rev,
        name=name,
        description=description,
        manufacturer=manufacturer,
        mpn=mpn,
        identifiers=parse_kv(ids, "--id"),
        uris=uris or [],
        attributes=dict(parse_kv(attrs, "--attr")),
    )
    typer.echo(f"added {node.key} rev {node.revision}  {node_hash}")


@command(part_app, "revise")
def part_revise(
    part_id: str,
    rev: Annotated[str, typer.Option("--rev", help="New revision label.")],
    name: Annotated[Optional[str], typer.Option("--name")] = None,
    description: DescOpt = None,
    manufacturer: MfrOpt = None,
    mpn: MpnOpt = None,
    ids: IdOpt = None,
    uris: UriOpt = None,
    attrs: AttrOpt = None,
    home: HomeOpt = None,
) -> None:
    """Create a new revision of a part; unspecified fields carry over."""
    s = Session(home)
    node_hash, node = parts.revise_part(
        s.store,
        s.actor,
        part_id,
        revision=rev,
        name=name,
        description=description,
        manufacturer=manufacturer,
        mpn=mpn,
        identifiers=parse_kv(ids, "--id") if ids is not None else None,
        uris=uris,
        attributes=dict(parse_kv(attrs, "--attr")) if attrs is not None else None,
    )
    typer.echo(f"revised {node.key} -> rev {node.revision}  {node_hash}")


@command(part_app, "show")
def part_show(
    part_ref: Annotated[str, typer.Argument(help="org:part_id, or part_id of the acting org.")],
    history: Annotated[bool, typer.Option("--history", help="List all revisions.")] = False,
    home: HomeOpt = None,
) -> None:
    """Show a part's current revision (and optionally its history)."""
    s = Session(home)
    org, part_id = parts.parse_part_ref(part_ref, s.config.effective_org)
    node_hash = parts.current_node(s.store, org, part_id)
    node = parts.load_node(s.store, node_hash)
    typer.echo(f'{node.key} rev {node.revision}  "{node.name}"')
    typer.echo(f"  node:         {node_hash}")
    for label, value in (
        ("description", node.description),
        ("manufacturer", node.manufacturer),
        ("mpn", node.mpn),
    ):
        if value:
            typer.echo(f"  {label + ':':<13} {value}")
    for scheme, value in node.identifiers:
        typer.echo(f"  {'identifier:':<13} {scheme}={value}")
    for uri in node.uris:
        typer.echo(f"  {'uri:':<13} {uri}")
    for key, value in sorted(node.attributes.items()):
        typer.echo(f"  {'attribute:':<13} {key}={value}")
    if history:
        typer.echo("history:")
        for r in parts.part_history(s.store, org, part_id):
            mark = "*" if r.current else " "
            typer.echo(f"  {mark} rev {r.node.revision:<6} {r.created_at}  {r.node_hash}")


@command(part_app, "list")
def part_list(
    org: Annotated[Optional[str], typer.Option("--org", help="Only this org's parts.")] = None,
    home: HomeOpt = None,
) -> None:
    """List parts (current revisions) across all orgs."""
    s = Session(home)
    # TODO(spec): lists every org's parts (everything is readable in v1); --org filters.
    for node_hash, node in parts.list_parts(s.store, org):
        typer.echo(f'{node.key}  rev {node.revision}  "{node.name}"  [{h(node_hash)}]')


# --------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------


@command(ws_app, "create")
def ws_create(
    ws_id: str,
    root: Annotated[str, typer.Option("--root", help="Root part ID (owned by the acting org).")],
    name: Annotated[Optional[str], typer.Option("--name")] = None,
    home: HomeOpt = None,
) -> None:
    """Create a workspace and make it current."""
    s = Session(home)
    ws = workspace.create_workspace(s.store, s.actor, ws_id, root, name)
    # TODO(spec): the spec doesn't say; a new workspace becomes the current one.
    s.config.use_workspace(ws.id)
    s.save()
    typer.echo(f"created workspace {ws.id} (owner {ws.owner_org_id}, root {ws.owner_org_id}:{ws.root_part_id})")


@command(ws_app, "list")
def ws_list(home: HomeOpt = None) -> None:
    """List all workspaces with their owner org (* = current)."""
    s = Session(home)
    for ws, head in workspace.list_workspaces(s.store):
        mark = "*" if ws.id == s.config.current_workspace else " "
        typer.echo(
            f"{mark} {ws.id}  owner {ws.owner_org_id}  root {ws.owner_org_id}:{ws.root_part_id}  HEAD {h(head)}"
        )


@command(ws_app, "use")
def ws_use(ws_id: str, home: HomeOpt = None) -> None:
    """Set the current workspace for the acting org."""
    s = Session(home)
    s.actor  # requires an acting org; the current workspace is kept per org
    workspace.get_workspace(s.store, ws_id)
    s.config.use_workspace(ws_id)
    s.save()
    typer.echo(f"using workspace {ws_id}")


# --------------------------------------------------------------------------
# Draft edges
# --------------------------------------------------------------------------


def _edge_line(owner: str, e: Any) -> str:
    child = e.child_key if e.is_external else f"{owner}:{e.child_part_id}"
    line = f"{owner}:{e.parent_part_id} -> {child}  {e.quantity} {e.uom}"
    if e.refdes:
        line += f"  refdes {compact_refdes(e.refdes)}"
    if e.is_external:
        line += f"  pinned {h(e.ext_configuration)}"
    return line


@command(edge_app, "add")
def edge_add(
    parent: str,
    child: str,
    qty: QtyOpt,
    uom: UomOpt = DEFAULT_UOM,
    refdes: RefdesOpt = None,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Add an edge from an own parent part to an own child part."""
    s = Session(home)
    actor = s.actor
    edge = workspace.add_edge(s.store, actor, s.ws(ws), parent, child, quantity=qty, uom=uom, refdes=refdes or [])
    typer.echo(f"added {_edge_line(actor.org, edge)}")


@command(edge_app, "add-external")
def edge_add_external(
    parent: str,
    source: Annotated[str, typer.Option("--from", help="Source workspace ID.")],
    qty: QtyOpt,
    commit: Annotated[Optional[str], typer.Option("--commit", help="Source revision to pin (default HEAD).")] = None,
    uom: UomOpt = DEFAULT_UOM,
    refdes: RefdesOpt = None,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Reference another workspace's configuration, pinned by hash."""
    s = Session(home)
    actor = s.actor
    edge = workspace.add_external_edge(
        s.store, actor, s.ws(ws), parent, source, quantity=qty, uom=uom, refdes=refdes or [], rev=commit
    )
    typer.echo(f"added {_edge_line(actor.org, edge)}")


@command(edge_app, "set")
def edge_set(
    parent: str,
    child_key: Annotated[str, typer.Argument(help="Own child part_id, or ext:<source_ws>.")],
    qty: Annotated[Optional[str], typer.Option("--qty")] = None,
    uom: Annotated[Optional[str], typer.Option("--uom")] = None,
    refdes: RefdesOpt = None,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Change an edge's quantity, unit, or reference designators."""
    s = Session(home)
    actor = s.actor
    edge = workspace.set_edge(s.store, actor, s.ws(ws), parent, child_key, quantity=qty, uom=uom, refdes=refdes)
    typer.echo(f"set {_edge_line(actor.org, edge)}")


@command(edge_app, "rm")
def edge_rm(
    parent: str,
    child_key: Annotated[str, typer.Argument(help="Own child part_id, or ext:<source_ws>.")],
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Remove an edge from the draft."""
    s = Session(home)
    actor = s.actor
    edge = workspace.remove_edge(s.store, actor, s.ws(ws), parent, child_key)
    typer.echo(f"removed {_edge_line(actor.org, edge)}")


@command(edge_app, "list")
def edge_list(ws: WsOpt = None, home: HomeOpt = None) -> None:
    """List the draft's edges."""
    s = Session(home)
    ws_obj = workspace.get_workspace(s.store, s.ws(ws))
    for e in s.store.list_draft_edges(ws_obj.id):
        typer.echo(_edge_line(ws_obj.owner_org_id, e))


# --------------------------------------------------------------------------
# History and views
# --------------------------------------------------------------------------


def _format_edge(info: Optional[EdgeInfo]) -> str:
    if info is None:
        return ""
    text = f"{info.quantity} {info.uom}"
    if info.refdes:
        text += f" refdes {compact_refdes(info.refdes)}"
    if info.source:
        text += f" ⟵ {info.source}"
    return text


def _print_diff(d: DiffResult, indent: str = "") -> None:
    if d.is_empty:
        typer.echo(f"{indent}no differences")
    if d.added_parts:
        typer.echo(f"{indent}Added parts:")
        for p in d.added_parts:
            typer.echo(f"{indent}  + {p.part} rev {p.revision}")
    if d.removed_parts:
        typer.echo(f"{indent}Removed parts:")
        for p in d.removed_parts:
            typer.echo(f"{indent}  - {p.part} rev {p.revision}")
    if d.revised_parts:
        typer.echo(f"{indent}Revised parts:")
        for c in d.revised_parts:
            fields = f"  (changed: {', '.join(c.fields)})" if c.fields else ""
            typer.echo(f"{indent}  ~ {c.part} rev {c.old_revision} -> {c.new_revision}{fields}")
    if d.edge_changes:
        typer.echo(f"{indent}Edge changes:")
        for e in d.edge_changes:
            if e.change == "added":
                typer.echo(f"{indent}  + {e.parent} -> {e.child}  {_format_edge(e.new)}")
            elif e.change == "removed":
                typer.echo(f"{indent}  - {e.parent} -> {e.child}  {_format_edge(e.old)}")
            else:
                changes = ", ".join(
                    f"refdes {refdes_change(e.old.refdes, e.new.refdes)}"
                    if f == "refdes"
                    else f"{f} {_value(getattr(e.old, f))} -> {_value(getattr(e.new, f))}"
                    for f in e.fields
                )
                typer.echo(f"{indent}  ~ {e.parent} -> {e.child}  {changes}")
    if d.opaque_changes:
        typer.echo(f"{indent}Opaque changes:")
        for o in d.opaque_changes:
            typer.echo(
                f"{indent}  ~ {o.parent} -> {o.child} ⟵ {o.source}: sub-assembly changed (contents not visible) "
                f"{h(o.old_configuration)} -> {h(o.new_configuration)}"
            )
    for m in d.missing:
        typer.echo(f"{indent}warning: object {m} is missing; the diff may be incomplete")


def _value(v: Any) -> str:
    if isinstance(v, tuple):
        return ",".join(v) or "-"
    return str(v) if v is not None else "-"


@command(app, "status")
def status_cmd(ws: WsOpt = None, home: HomeOpt = None) -> None:
    """Compare the draft with HEAD."""
    s = Session(home)
    st = compute_status(s.store, s.ws(ws))
    w = st.workspace
    typer.echo(f"workspace {w.id} (owner {w.owner_org_id}, root {w.owner_org_id}:{w.root_part_id})")
    typer.echo(f"HEAD {st.head}" if st.head else "no commits yet")
    if st.build_error:
        typer.secho(f"draft cannot be committed: {st.build_error}", fg=typer.colors.RED)
    elif st.clean:
        typer.echo("draft matches HEAD (nothing to commit)")
    else:
        typer.echo("draft changes not committed:")
        _print_diff(st.diff, indent="  ")
    if st.outdated:
        typer.echo("outdated externals in draft (run `update`):")
        for e in st.outdated:
            typer.echo(f"  {_external_line(e)}")
    for warning in st.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)


@command(app, "commit")
def commit_cmd(
    message: Annotated[str, typer.Option("-m", "--message", help="Commit message.")],
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Snapshot the draft into immutable objects and move main."""
    s = Session(home)
    result = workspace.commit(s.store, s.actor, s.ws(ws), message)
    for warning in result.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
    typer.echo(f"[{result.commit.workspace} {h(result.hash)}] {message}")
    typer.echo(f"root configuration {result.commit.configuration}")


@command(app, "log")
def log_cmd(
    limit: Annotated[Optional[int], typer.Option("--limit", "-n", help="Show at most N commits.")] = None,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Show commit history of main."""
    s = Session(home)
    for i, (commit_hash, c) in enumerate(workspace.log(s.store, s.ws(ws), limit)):
        if i:
            typer.echo("")
        author = c.author_org + (f" ({c.author_user})" if c.author_user else "")
        typer.secho(f"commit {commit_hash}", fg=typer.colors.YELLOW)
        typer.echo(f"Author: {author}")
        typer.echo(f"Date:   {c.timestamp}")
        typer.echo(f"Root:   {c.configuration}")
        typer.echo("")
        typer.echo(f"    {c.message}")


def _resolve_any(s: Session, ref: str, ws: Optional[str]) -> str:
    if ref.startswith("HEAD"):
        return workspace.resolve_commit(s.store, s.ws(ws), ref)
    return workspace.resolve_hash(s.store, ref)


def _get_bytes(s: Session, object_hash: str) -> bytes:
    data = s.store.get_object(object_hash)
    if data is None:
        raise OpenBomError(f"object {object_hash} not found")
    return data


@command(app, "show")
def show_cmd(
    ref: Annotated[str, typer.Argument(help="HEAD, HEAD~N, a hash, or a unique hash prefix.")],
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Pretty-print any object (debugging; ignores opacity)."""
    s = Session(home)
    object_hash = _resolve_any(s, ref, ws)
    body = decode(_get_bytes(s, object_hash)).body()
    typer.echo(f"{body['type']} {object_hash}")
    echo_json(body)


@command(app, "cat-object")
def cat_object(
    ref: Annotated[str, typer.Argument(help="A hash or unique hash prefix.")],
    home: HomeOpt = None,
) -> None:
    """Print an object's exact stored canonical bytes."""
    s = Session(home)
    typer.echo(_get_bytes(s, workspace.resolve_hash(s.store, ref)).decode("utf-8"))


def _commit_configuration(s: Session, ws: Optional[str], rev: str) -> str:
    commit_hash = workspace.resolve_commit(s.store, ws if not rev.startswith("HEAD") else s.ws(ws), rev)
    return workspace.load_commit(s.store, commit_hash).configuration


@command(app, "tree")
def tree_cmd(
    rev: Annotated[str, typer.Argument(help="Commit (default HEAD).")] = "HEAD",
    depth: Annotated[Optional[int], typer.Option("--depth", help="Limit depth.")] = None,
    hashes: Annotated[bool, typer.Option("--hashes", help="Show full hashes.")] = False,
    reveal: RevealOpt = False,
    as_json: JsonOpt = False,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Render a commit's configuration as an indented tree."""
    s = Session(home)
    root = _commit_configuration(s, ws, rev)
    tree = tree_render.build_tree(s.store.get_object, root, reveal=reveal, max_depth=depth)
    if as_json:
        echo_json(tree)
    else:
        typer.echo(tree_render.render_text(tree, full_hashes=hashes))


@command(app, "diff")
def diff_cmd(
    a: Annotated[Optional[str], typer.Argument(help="Old commit (default HEAD~1).")] = None,
    b: Annotated[Optional[str], typer.Argument(help="New commit (default HEAD).")] = None,
    reveal: RevealOpt = False,
    as_json: JsonOpt = False,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Compute the difference between two commits."""
    s = Session(home)
    new = _commit_configuration(s, ws, b or "HEAD")
    if a is None:
        # TODO(spec): with a single commit, HEAD~1 doesn't exist; diff against an empty graph.
        head_commit = workspace.load_commit(s.store, workspace.resolve_commit(s.store, s.ws(ws), "HEAD"))
        old = _commit_configuration(s, ws, "HEAD~1") if head_commit.parents else None
    else:
        old = _commit_configuration(s, ws, a)
    result = diff_configurations(s.store.get_object, old, new, reveal=reveal)
    if as_json:
        echo_json(result.to_json())
    else:
        _print_diff(result)


def _external_line(e: sync.ExternalStatus) -> str:
    where = f"{e.part} ⟵ {e.source} (in {e.parent})"
    if e.status == sync.SOURCE_NOT_FOUND:
        return f"{where}: source not found"
    if e.status == sync.NO_COMMITS:
        return f"{where}: source has no commits"
    return (
        f"{where}: rev {e.pinned_revision} {h(e.pinned_configuration)} -> "
        f"rev {e.latest_revision} {h(e.latest_configuration)}"
    )


@command(app, "outdated")
def outdated_cmd(ws: WsOpt = None, home: HomeOpt = None) -> None:
    """List external entries in HEAD whose source has moved on."""
    s = Session(home)
    results = sync.outdated(s.store, s.ws(ws))
    if not results:
        typer.echo("all external entries are up to date")
    for e in results:
        typer.echo(_external_line(e))


@command(app, "update")
def update_cmd(
    part: Annotated[Optional[str], typer.Argument(help="org:part_id of the external entry.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Update every external entry.")] = False,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Move pinned external entries in the draft to their source's latest commit."""
    s = Session(home)
    result = sync.update(s.store, s.actor, s.ws(ws), part, all=all_)
    for e in result.updated:
        typer.echo(f"updated {_external_line(e)}")
    for e in result.skipped:
        typer.secho(f"skipped {_external_line(e)}", fg=typer.colors.YELLOW)
    if result.updated:
        typer.echo("draft updated; run `commit` to record it")
    elif not result.skipped:
        typer.echo("already up to date")


@command(app, "verify")
def verify_cmd(
    rev: Annotated[str, typer.Argument(help="Commit (default HEAD).")] = "HEAD",
    reveal: RevealOpt = False,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Rehash and schema-check every object reachable from a commit."""
    s = Session(home)
    commit_hash = workspace.resolve_commit(s.store, s.ws(ws) if rev.startswith("HEAD") else ws, rev)
    report = verify_commit(
        s.store.get_object,
        commit_hash,
        reveal=reveal,
        source_history=lambda source: workspace.history_configurations(s.store, source),
    )
    typer.echo(f"verified {report.objects_verified} objects from commit {h(commit_hash)}")
    typer.echo(f"opaque configurations (not traversed): {report.opaque}")
    if report.ok:
        typer.secho("OK", fg=typer.colors.GREEN)
        return
    typer.secho(f"{len(report.failures)} failure(s):", fg=typer.colors.RED)
    for failure in report.failures:
        typer.echo(f"  {failure}")
    raise typer.Exit(1)


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


def _print_plan(plan: ImportPlan) -> None:
    target = f"new workspace {plan.workspace}" if plan.create_workspace else f"workspace {plan.workspace}"
    where = f"{plan.file}" + (f" [sheet {plan.sheet}]" if plan.sheet else "")
    typer.echo(f"Import {where} [{plan.profile} profile] -> {target}" + (f" (root {plan.root})" if plan.root else ""))
    cols = ", ".join(f"{f}={h!r}" for f, h in plan.columns.items())
    typer.echo(f"  header row {plan.header_row}: {cols}")
    if plan.ignored_columns:
        typer.echo(f"  ignored columns: {', '.join(plan.ignored_columns)}")
    counts = {a: sum(1 for p in plan.parts if p.action == a) for a in ("new", "revise", "unchanged")}
    typer.echo(
        f"  {plan.row_count} rows, {len(plan.parts)} parts: {counts['new']} new, "
        f"{counts['revise']} revised, {counts['unchanged']} unchanged"
    )
    for p in plan.parts:
        if p.action == "new":
            typer.echo(f'    + {p.part_id} rev {p.revision}  "{p.name}"')
        elif p.action == "revise":
            typer.echo(f"    ~ {p.part_id} rev {p.old_revision} -> {p.revision}  (changed: {', '.join(p.fields)})")
    for e in plan.edges:
        if e.external:
            pin = h(e.pin_configuration) + (" (existing pin kept)" if e.pin_kept else " (source HEAD)")
            typer.echo(f"    ⟵ {e.parent} -> {e.quantity} {e.uom} from workspace {e.child}, pinned {pin}")
    replaced = f"; {plan.removed_draft_edges} current draft edge(s) removed" if plan.removed_draft_edges else ""
    typer.echo(f"  draft: {len(plan.edges)} edges{replaced}")
    if plan.preview is not None:
        typer.echo("If applied and committed, compared with HEAD:")
        _print_diff(plan.preview, indent="  ")
    if plan.issues:
        typer.echo("Issues:")
        colors = {ERROR: typer.colors.RED, WARNING: typer.colors.YELLOW}
        for i in plan.issues:
            where = f"row {i.line}" if i.line else ""
            typer.secho(f"  {i.severity:<7} {where:<8} {i.message}", fg=colors.get(i.severity))
            if i.hint:
                typer.echo(f"  {'':<16} -> {i.hint}")


@command(app, "import")
def import_cmd(
    file: Annotated[Path, typer.Argument(help="Indented BOM export (.csv, .tsv, or .xlsx).")],
    new_ws: Annotated[
        Optional[str], typer.Option("--new-ws", help="Create this workspace, rooted at the file's top row.")
    ] = None,
    sheet: Annotated[Optional[str], typer.Option("--sheet", help="Worksheet name (.xlsx).")] = None,
    profile: Annotated[
        Optional[str], typer.Option("--profile", help="Source layout: agile, fusion, or generic (default: detect).")
    ] = None,
    column: Annotated[
        Optional[list[str]],
        typer.Option("--column", help='Map a header the importer misses: field="Header text". Repeatable.'),
    ] = None,
    update_metadata: Annotated[
        bool, typer.Option("--update-metadata", help="Allow changed fields without a new revision label.")
    ] = False,
    apply: Annotated[bool, typer.Option("--apply", help="Write parts and replace the draft (otherwise a dry run).")] = False,
    as_json: JsonOpt = False,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Plan (and with --apply, perform) a BOM import into a workspace draft. Never commits."""
    s = Session(home)
    actor = s.actor
    plan, result = run_import(
        s.store,
        actor,
        file,
        apply=apply,
        ws_id=None if new_ws else s.ws(ws),
        new_ws=new_ws,
        sheet=sheet,
        columns=dict(parse_kv(column, "--column")),
        update_metadata=update_metadata,
        profile=profile,
    )
    if result is not None and result.created_workspace:
        s.config.use_workspace(plan.workspace)
        s.save()
    if as_json:
        echo_json({**plan.to_json(), "applied": result is not None})
    else:
        _print_plan(plan)
        if plan.errors:
            typer.secho(f"Not applied: {len(plan.errors)} error(s). Fix them and re-run.", fg=typer.colors.RED)
        elif result is None:
            typer.echo("Dry run. Re-run with --apply to write parts and the draft.")
        else:
            typer.secho(
                f"Applied: {result.parts_written} part revisions written, draft has {result.edges} edges.",
                fg=typer.colors.GREEN,
            )
            typer.echo('Next: openbom status, then openbom commit -m "<message>"')
    if plan.errors:
        raise typer.Exit(1)


@command(map_app, "add")
def map_add(
    source_pn: Annotated[str, typer.Argument(help="Part number exactly as it appears in the file.")],
    part: Annotated[Optional[str], typer.Option("--part", help="Treat it as this own part ID.")] = None,
    source: Annotated[
        Optional[str], typer.Option("--from", help="Treat it as this workspace's assembly (external entry).")
    ] = None,
    ignore: Annotated[bool, typer.Option("--ignore", help="Skip it and everything under it.")] = False,
    home: HomeOpt = None,
) -> None:
    """Record what a part number in imported files means (applies to every later import)."""
    s = Session(home)
    m = ingest_mappings.set_mapping(s.store, s.actor, source_pn, part_id=part, ext_source_ws=source, ignore=ignore)
    typer.echo(f"mapped {m.source_pn!r} -> {ingest_mappings.describe(m)}")


@command(map_app, "list")
def map_list(home: HomeOpt = None) -> None:
    """List the acting org's import mappings."""
    s = Session(home)
    for m in ingest_mappings.list_mappings(s.store, s.actor):
        typer.echo(f"{m.source_pn!r} -> {ingest_mappings.describe(m)}")


@command(map_app, "rm")
def map_rm(source_pn: str, home: HomeOpt = None) -> None:
    """Remove an import mapping."""
    s = Session(home)
    ingest_mappings.remove_mapping(s.store, s.actor, source_pn)
    typer.echo(f"removed mapping for {source_pn!r}")


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

KIND_LABEL = {
    "match": "match", "regrouped": "regrouped", "moved": "moved", "quantity_mismatch": "quantity",
    "identity_conflict": "identity", "sourcing": "sourcing",
}


def _who(plan: "reconcile.ReconcilePlan", keys: list[str], width: int = 28) -> str:
    def one(k: str) -> str:
        name = plan.names.get(k, "")
        return f"{k} \"{name[:width]}{'…' if len(name) > width else ''}\"" if name else k

    return " + ".join(one(k) for k in keys)


def _finding_line(plan: "reconcile.ReconcilePlan", f: "reconcile.Finding") -> str:
    a, b = plan.left["source"], plan.right["source"]
    label = KIND_LABEL.get(f.kind) or f"only in {a if f.kind == 'left_only' else b}"
    pair = " <-> ".join(x for x in (_who(plan, f.left), _who(plan, f.right)) if x)
    if f.kind == "quantity_mismatch":
        what = f"under {f.anchor}: {a} {f.left_qty}, {b} {f.right_qty}"
    elif f.kind in ("match", "regrouped"):
        what = f"{f.left_qty} = {f.right_qty} under {f.anchor}" + (" (via different sub-assemblies)" if f.kind == "regrouped" else "")
    elif f.kind == "left_only":
        what = f"{f.left_qty} under {f.anchor}"
    elif f.kind == "right_only":
        what = f"{f.right_qty} under {f.anchor}"
    else:
        what = f.detail
    accepted = f"  [accepted: {f.accepted}]" if f.accepted else ""
    return f"  {label:<20} {pair}: {what}{accepted}"


def _print_recon_plan(plan: "reconcile.ReconcilePlan", show_all: bool) -> None:
    a, b = plan.left["source"], plan.right["source"]
    typer.echo(f"Reconciliation {plan.workspace}: {a} <-> {b}")
    for d in (plan.left, plan.right):
        typer.echo(f"  {d['source']} @ {h(d['configuration'])}  (root {_who(plan, [d['root']], 40)})")
    label = lambda k: {"left_only": f"only in {a}", "right_only": f"only in {b}"}.get(k, KIND_LABEL.get(k, k))  # noqa: E731
    counts = ", ".join(f"{v} {label(k.split(' ')[0])}{' (accepted)' if 'accepted' in k else ''}" for k, v in plan.counts().items())
    typer.echo(f"Pairs: {len(plan.pairs)}  |  {counts}")
    if plan.proposals:
        typer.secho(f"Needs a decision ({len(plan.proposals)}):", fg=typer.colors.YELLOW)
        for p in plan.proposals:
            cands = "; ".join(f"{_who(plan, [r])} [{', '.join(ev)}]" for r, ev in p.candidates)
            typer.echo(f"  ? {_who(plan, [p.left])} -- {p.reason} evidence for: {cands}")
            typer.echo(f"      -> openbom reconcile pair {p.left} {p.candidates[0][0]}   (or: reconcile reject ...)")
    warnings = [f for f in plan.findings if f.severity == "warning"]
    if warnings:
        typer.echo("Differences:")
        for f in warnings:
            typer.secho(_finding_line(plan, f), fg=None if f.accepted else typer.colors.YELLOW)
            if f.kind == "left_only" and not f.accepted:
                typer.echo(f"      -> openbom reconcile accept {f.left[0]} --note \"<why>\"")
    info = [f for f in plan.findings if f.severity == "info" and (show_all or f.kind != "right_only")]
    if info:
        typer.echo("Info:")
        for f in info:
            typer.echo(_finding_line(plan, f))
    hidden = [f for f in plan.findings if f.kind == "right_only"]
    if hidden and not show_all:
        typer.echo(f"  {'only in ' + b:<20} {len(hidden)} lines {a} doesn't list (e.g. screws, labels); --all to list")
    if show_all:
        for f in plan.findings:
            if f.kind == "match":
                typer.echo(_finding_line(plan, f))
    if plan.head and (plan.new_findings or plan.resolved_findings):
        typer.echo("Changes since the last reconciliation commit:")
        for f in plan.new_findings:
            typer.secho("  +" + _finding_line(plan, f)[1:], fg=typer.colors.RED)
        for f in plan.resolved_findings:
            typer.secho("  -" + _finding_line(plan, f)[1:], fg=typer.colors.GREEN)
    typer.echo(f"Correspondence if committed: {h(plan.correspondence)}")


@command(recon_app, "init")
def recon_init(
    ws_id: str,
    side_a: Annotated[str, typer.Argument(help="One workspace (e.g. the customer's).")],
    side_b: Annotated[str, typer.Argument(help="The other workspace (e.g. the ODM's).")],
    approver: Annotated[
        Optional[str],
        typer.Option("--approver", help="Workspace whose approved manufacturer lists are authoritative "
                     "(default: the side your org owns)."),
    ] = None,
    strip_prefix: Annotated[
        Optional[list[str]],
        typer.Option("--strip-prefix", help="WORKSPACE=PREFIX: ignore a site prefix on that side's part numbers, e.g. odm-tray=MT."),
    ] = None,
    name: Annotated[Optional[str], typer.Option("--name")] = None,
    home: HomeOpt = None,
) -> None:
    """Create a reconciliation workspace (owned by your org) pinning both sides at their HEADs."""
    s = Session(home)
    prefixes: dict[str, list[str]] = {}
    for ws, prefix in parse_kv(strip_prefix, "--strip-prefix"):
        prefixes.setdefault(ws, []).append(prefix)
    chosen = reconcile.init_reconciliation(
        s.store, s.actor, ws_id, side_a, side_b, name=name, strip_prefix=prefixes, approver=approver
    )
    s.config.use_workspace(ws_id)
    s.save()
    typer.echo(f"created reconciliation {ws_id}: {side_a} <-> {side_b}, pinned at their HEADs")
    typer.echo(f"approved manufacturer lists come from: {chosen or '- (no sourcing check; use --approver)'}")
    typer.echo("Next: openbom reconcile status")


@command(recon_app, "status")
def recon_status(
    show_all: Annotated[bool, typer.Option("--all", help="Also list matches and one-sided detail lines.")] = False,
    as_json: JsonOpt = False,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Match the pinned sides and report pairs, proposals, and differences (writes nothing)."""
    s = Session(home)
    plan = reconcile.plan_reconciliation(s.store, s.ws(ws))
    if as_json:
        echo_json(plan.to_json())
    else:
        _print_recon_plan(plan, show_all)


def _decide(kind: str, parts: list[str], note: str, ws: Optional[str], home: Optional[Path]) -> None:
    s = Session(home)
    keys = reconcile.decide(s.store, s.actor, s.ws(ws), kind, parts, note)
    typer.echo(f"recorded {kind}: " + " <-> ".join(keys))


@command(recon_app, "pair")
def recon_pair(part_a: str, part_b: str, ws: WsOpt = None, home: HomeOpt = None) -> None:
    """Confirm two parts (one from each side, either order) are the same item."""
    _decide("pair", [part_a, part_b], "", ws, home)


@command(recon_app, "reject")
def recon_reject(part_a: str, part_b: str, ws: WsOpt = None, home: HomeOpt = None) -> None:
    """Rule out a proposed pairing."""
    _decide("reject", [part_a, part_b], "", ws, home)


@command(recon_app, "accept")
def recon_accept(
    part: str,
    note: Annotated[str, typer.Option("--note", help="Why this difference is acceptable.")] = "",
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Accept a difference involving one part (e.g., a customer-consigned item the ODM doesn't list)."""
    _decide("accept", [part], note, ws, home)


@command(recon_app, "commit")
def recon_commit(
    message: Annotated[str, typer.Option("-m", "--message")],
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Commit the current correspondence (pins + agreed pairs + accepted differences)."""
    s = Session(home)
    result = reconcile.commit_reconciliation(s.store, s.actor, s.ws(ws), message)
    typer.echo(f"[{result.commit.workspace} {h(result.hash)}] {message}")
    typer.echo(f"correspondence {result.commit.correspondence}")


@command(recon_app, "diff")
def recon_diff(
    a: Annotated[Optional[str], typer.Argument(help="Old reconciliation commit (default HEAD~1).")] = None,
    b: Annotated[Optional[str], typer.Argument(help="New reconciliation commit (default HEAD).")] = None,
    ws: WsOpt = None,
    home: HomeOpt = None,
) -> None:
    """Show which differences appeared or were resolved between two reconciliation commits."""
    s = Session(home)
    ws_id = s.ws(ws)
    new_hash = reconcile.resolve_recon_commit(s.store, ws_id, b or "HEAD")
    old_hash = reconcile.resolve_recon_commit(s.store, ws_id, a or "HEAD~1")
    plan = reconcile.plan_reconciliation(s.store, ws_id)  # for names
    new, resolved = reconcile.diff_reconciliations(s.store, old_hash, new_hash)
    typer.echo(f"Reconciliation {ws_id}: {h(old_hash)} -> {h(new_hash)}")
    if not new and not resolved:
        typer.echo("  no change in differences")
    for f in new:
        typer.secho("  +" + _finding_line(plan, f)[1:], fg=typer.colors.RED)
    for f in resolved:
        typer.secho("  -" + _finding_line(plan, f)[1:], fg=typer.colors.GREEN)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
