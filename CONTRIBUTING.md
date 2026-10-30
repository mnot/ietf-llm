# Contributing

Contributions — code, bugs, or ideas — are very welcome.

## Intellectual Property

By contributing code, bugs or enhancements to this project (whether through pull requests, the
issues list, e-mail or other means), you are licensing your contribution under the
[project's terms](LICENSE.md).

## Coding Conventions

Python is formatted with [black](https://pypi.org/project/black/) (`make tidy`, which covers
`ietf_llm/` but not `tests/`). All functions and methods need type annotations; the pylint and mypy
settings are in `.pylintrc` and `mypy.ini`.

## Setting Up a Development Environment

Any modern Unix-like environment with a recent Python will do. [Makefile.venv](https://github.com/sio/Makefile.venv)
creates a virtual environment and installs dependencies the first time you run `make`.

Useful targets:

* `make shell` — a shell in the virtual environment
* `make python` — an interactive interpreter in the virtual environment
* `make lint` — run pylint
* `make typecheck` — run mypy
* `make tidy` — format the source
* `make test` — run the tests

[docs/architecture.md](docs/architecture.md) has a map of the packages.

## Before you Submit

The best way to submit a change is a pull request. Before you open one:

* Run `make tidy`.
* Run `make lint` and `make typecheck`, and address anything they report. Pylint must exit cleanly.
* Run `make test`, and add tests for new behaviour.
* Update the documentation that describes what you changed.

If you're not sure how to dig in, ask for help, or sketch out an idea in an issue first.
