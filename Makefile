PROJECT = ietf_llm


.PHONY: clean
clean: clean_py

.PHONY: lint
lint: lint_py

.PHONY: typecheck
typecheck: typecheck_py

.PHONY: tidy
tidy: tidy_py

.PHONY: test
test: venv
	$(VENV)/python -m pytest tests/

# Re-vendor the Agent Skills from mnot/ietf-skill. `make vendor-skills` tracks
# the newest upstream tag; `make vendor-skills REF=v0.4.1` pins one. Needs `gh`.
.PHONY: vendor-skills
vendor-skills:
	./scripts/vendor-skills.sh $(REF)

# Verify the vendored skills still match the tag pinned in VENDORED.md.
# The script exits 1 for drift and 2 for "upstream unreachable"; make collapses
# every recipe failure to its own exit 2, so the distinction has to be acted on
# here rather than by the caller. Unreachable is not a failure -- a check that
# cries wolf when GitHub has a bad minute is one people stop reading.
.PHONY: vendor-skills-check
vendor-skills-check:
	@./scripts/vendor-skills.sh --check; code=$$?; \
	if [ $$code -eq 2 ]; then \
		echo "upstream unreachable; vendored skills not checked"; \
		[ -n "$$GITHUB_ACTIONS" ] && \
			echo "::warning::vendored-skill pin not checked (upstream unreachable)"; \
		exit 0; \
	fi; \
	exit $$code

include Makefile.pyproject
