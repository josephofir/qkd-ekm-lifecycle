.PHONY: setup test lint dist model analysis experiment redact-check tf-validate

setup:
	uv sync --extra dev

test:
	uv run pytest -q

lint:
	uv run ruff check .

dist:
	scripts/build_dist.sh

model:
	uv run qkd-ekm-model all

analysis:
	uv run qkd-ekm-analysis all

# End-to-end evidence run against the deployed stack (see README, "Quick start").
experiment:
	scripts/run_experiment.sh all

# Re-redact the most recent run and fail if anything unredacted survives.
redact-check:
	scripts/run_experiment.sh redact

tf-validate:
	terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate
