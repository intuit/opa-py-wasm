# Contributing to opa-py-wasm

Thanks for your interest in contributing! Whether it's improving documentation,
fixing a bug, or proposing a feature, contributions are welcome.

- [Reporting bugs and requesting features](#reporting-bugs-and-requesting-features)
- [Development setup](#development-setup)
- [Code quality expectations](#code-quality-expectations)
- [Contribution process](#contribution-process)
- [Contact](#contact)

## Reporting bugs and requesting features

Please open a [GitHub issue](https://github.com/intuit/opa-py-wasm/issues).

For bug reports, include as much detail as you can: the version of
`opa-py-wasm`, your Python version and OS, a minimal reproduction, and the full
traceback. If the bug involves a specific policy, a minimal `.rego` source that
reproduces it is enormously helpful.

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) to manage dependencies.

```shell
# Install uv (see https://docs.astral.sh/uv/getting-started/installation/)
brew install uv          # macOS; other options in the uv docs

git clone https://github.com/intuit/opa-py-wasm.git
cd opa-py-wasm
uv sync --all-extras --all-groups
```

Run the test suite:

```shell
uv run pytest
```

Run the full matrix and linters exactly as CI does:

```shell
uv run tox
```

> The first `tox` run downloads managed Python interpreters for each version in
> the matrix, so it takes noticeably longer than subsequent runs.

### Test fixtures

Compiled `.wasm` fixtures are committed under `tests/fixtures/wasm`, so the test
suite runs without the OPA CLI. Tests marked `requires_wasm` are skipped if
those fixtures are absent. To regenerate them from the Rego sources in
`tests/fixtures/rego`, install the
[OPA CLI](https://www.openpolicyagent.org/docs/latest/#running-opa) and run:

```shell
python scripts/build_fixtures.py            # all fixtures
python scripts/build_fixtures.py allow_true # a single fixture
```

## Code quality expectations

All of the following are enforced in CI, and a pull request must pass them:

- **Tests** — new features and bug fixes should come with tests. A bug fix
  should include a test that fails before the fix and passes after it.
- **Coverage** — library coverage must stay at or above **90%**
  (`fail_under` in `[tool.coverage.report]`). The suite currently sits at ~99%.
- **Formatting** — `black` and `isort`, line length 120.
- **Linting** — `ruff`.
- **Types** — `mypy`. The library is fully typed and ships `py.typed`; new
  public functions need annotations.

Run everything locally before pushing:

```shell
uv run ruff check .
uv run isort --check-only --df .
uv run black --check --diff .
uv run mypy
uv run pytest --cov=opapywasm --cov-report=term
```

- **Documentation** — code should explain *why*, not restate *what*. Public API
  changes should be reflected in [`docs/`](./docs) and the
  [`README`](./README.md).

## Contribution process

Contributions are made through a fork and pull request.

1. Fork the repository on GitHub, then clone your fork and add the upstream
   remote:

   ```shell
   git clone git@github.com:<your-username>/opa-py-wasm.git
   cd opa-py-wasm
   git remote add upstream https://github.com/intuit/opa-py-wasm.git
   ```

2. Create a branch with a descriptive name:

   ```shell
   git checkout -b fix/pool-close-race
   ```

3. Make your changes, including tests and documentation. Write commit messages
   that describe what changed and why.

4. Keep your branch current with `rebase` rather than `merge`:

   ```shell
   git fetch upstream
   git rebase upstream/main
   git push origin <your-branch>
   ```

5. Open a pull request against `master` and fill out the template. Link the
   issue it addresses (e.g. `Closes #123`). CI runs automatically.

6. A maintainer will review your change and may suggest adjustments. Once it is
   approved and CI is green, it will be merged and included in the next release.

## Contact

The best place to reach the maintainers is a
[GitHub issue](https://github.com/intuit/opa-py-wasm/issues) or a
[discussion](https://github.com/intuit/opa-py-wasm/discussions). Current
maintainers are listed in [`.github/CODEOWNERS`](./.github/CODEOWNERS).

By contributing, you agree that your contributions will be licensed under the
[MIT License](./LICENSE), and that you will abide by the
[Code of Conduct](./CODE_OF_CONDUCT.md).
