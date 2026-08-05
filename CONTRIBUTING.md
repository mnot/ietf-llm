# Contributing

Contributions - in the form of code, bugs, or ideas - are very welcome!

## Intellectual Property

By contributing code, bugs or enhancements to this project (whether that be through pull requests, the issues list, e-mail or other means), you are licensing your contribution under the [project's terms](LICENSE.md).


## Coding Conventions

We use [black](https://pypi.org/project/black/) for Python formatting, which can be run with `make tidy`.

All Python functions and methods need to have type annotations. See `pyproject.toml` for specific pylint and mypy settings.


## Setting up a Development Environment

It should be possible to use modern Unix-like environment, provided that a recent release of Python is installed.

Thanks to [Makefile.venv](https://github.com/sio/Makefile.venv), a Python virtual environment is set up and run each time you use `make`. As long as you use `make`, Python dependencies will be installed automatically.

Helpful make targets include:

* `make shell` - start a shell in the Python virtual environment
* `make python` - start an interactive Python interpreter in the virtual environment
* `make lint` - run pylint with REDbot-specific configuration
* `make typecheck` - run mypy to check Python types
* `make tidy` - format Python source
* `make test` - run the tests


## Before you Submit

The best way to submit a change is through a pull request. A few things to keep in mind when you're doing so:

* Run `make tidy`.
* Check your code with `make lint` and address any issues found.
* Check your code with `make typecheck` and address any issues found.
* Every new field and every new `Note` should have a test covering it.

### If you changed the MCP surface

The server `instructions` and the tool descriptions are context every client
pays for before it asks anything — currently around 19k tokens. `make test`
holds a ceiling on that (`tests/test_mcp_surface_budget.py`); if you widened a
docstring, it fails and tells you how to regenerate the baseline in the same
commit, so the cost shows up in review. Three tools help you decide what to cut:

* `.venv/bin/python scripts/mcp_surface_report.py` — where the weight is, per
  tool, in tokens; plus which phrasing is repeated across descriptions.
* `scripts/lint-prose.sh` — [vale](https://vale.sh) over the instructions and
  the extracted docstrings (`brew install vale`). Advisory, not part of the gate.
* `.venv/bin/python scripts/mcp_tool_similarity.py` — which descriptions a model
  can't tell apart. Needs the embedding model, so it doesn't run in CI.

If you're not sure how to dig in, feel free to ask for help, or sketch out an idea in an issue first.

