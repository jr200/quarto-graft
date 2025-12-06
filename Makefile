DOCS_DIR := docs

.PHONY: all env build render preview clean clean-grafts clean-all

all: render

## Setup Python deps (bookbuilder, PyYAML, etc.)
env:
	@echo "Syncing uv environment..."
	uv venv --clear && uv sync

## Linting
.PHONY: lint
lint:
	@echo "Running ruff..."
	uv run ruff check . --fix

## Render the main Quarto document
render:
	@echo "Rendering main Quarto project in $(DOCS_DIR)/..."
	uv run quarto render "$(DOCS_DIR)" --no-execute

## Preview the composed document (builds grafts first)
preview: render
	@echo "Starting Quarto preview for $(DOCS_DIR)/..."
	uv run quarto preview "$(DOCS_DIR)"

## Clean build artifacts
clean:
	@echo "Cleaning Quarto build artifacts..."
	rm -rf "$(DOCS_DIR)/_site" "$(DOCS_DIR)/.quarto"


clean-all: clean
	rm -rf .venv .ruff_cache .mypy_cache
	find . -type f -name '*.py[co]' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '.mypy_cache' -print0 | xargs -0 rm -rf
