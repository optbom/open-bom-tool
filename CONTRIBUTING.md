# Contributing

Thanks for helping. openbom is an early prototype, so design feedback is as valuable as code.

## Getting started

```sh
git clone https://github.com/optbom/open-bom-tool.git
cd open-bom-tool
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Python 3.11 or newer. [docs/overview.md](docs/overview.md) explains what the project is for, and [docs/concepts.md](docs/concepts.md) defines its terms.

## Ways to help

- **Report an issue.** Include the command, what you expected, and what happened.
- **Share a BOM layout we can't read.** A new PLM or spreadsheet layout is often the most useful contribution. **Never share real BOMs.** Proprietary part numbers, suppliers, and quantities are confidential. Replace them with fake values that keep the file's *shape*: headers, title rows, level format, and how alternates and manufacturers are laid out.
- **Add an import profile.** See `src/openbom/ingest/profiles/`. A profile turns one layout into a `SourceBOM`; add a small synthetic fixture and tests like those in `tests/plm_fixtures.py`.
- **Improve matching.** Reconciliation evidence and difference classes live in `src/openbom/reconcile.py`.

## Ground rules for code

- **The core library never imports the CLI.** `cli.py` parses arguments and formats output only, so an HTTP API can wrap the same functions later.
- **All SQL lives in `store.py`**, and every object read goes through `Store.get_object`.
- **Hashed formats are a contract.** Any change to how an object is serialized or hashed changes every hash. Add new fields as optional and omitted when empty, keep `tests/test_hashing.py`'s golden vector passing, and call out format changes in your PR.
- **Database changes need a migration** in `store.py` (`MIGRATIONS`), plus a test.
- **Errors are specific.** Name the org, part, workspace, or cycle involved; library errors subclass `OpenBomError`.
- Match the surrounding style. Where you make a judgment call the design doesn't settle, leave a `TODO(spec):` comment explaining it.

## Pull requests

1. Branch from `main`.
2. Add or update tests; `pytest` must pass.
3. Keep PRs focused, and describe the *why* as well as the what.
4. Update the README or docs when you change commands or concepts.

## License

openbom is licensed under the [Apache License 2.0](LICENSE). By contributing, you agree that your contributions are licensed under the same terms.
