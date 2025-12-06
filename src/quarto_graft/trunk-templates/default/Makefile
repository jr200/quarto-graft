DOCS_DIR := .
WORKTREES_DIR := .grafts-cache

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

## Assemble main document from grafts
build:
	@echo "Grafting branches onto the trunk..."
	uv run quarto-graft trunk build

## Render the main Quarto document
render: build
	@echo "Rendering main Quarto project in $(DOCS_DIR)/..."
	uv run quarto render "$(DOCS_DIR)" --no-execute

## Preview the composed document (builds grafts first)
preview: build
	@echo "Starting Quarto preview for $(DOCS_DIR)/..."
	uv run quarto preview "$(DOCS_DIR)"

## Clean build artifacts
clean:
	@echo "Cleaning Quarto build artifacts..."
	rm -rf "$(DOCS_DIR)/_site" "$(DOCS_DIR)/.quarto" "$(DOCS_DIR)/grafts__" 

clean-grafts:
	@echo "Ensuring graft worktrees are clean before removal..."
	@set -e; \
	if [ -d "$(WORKTREES_DIR)" ]; then \
		wts=$$(git worktree list --porcelain | awk '/^worktree /{print $$2}' | grep '^$(WORKTREES_DIR)/' || true); \
		for wt in $$wts; do \
			if [ -n "$$(git -C "$$wt" status --porcelain)" ]; then \
				echo "Worktree has uncommitted changes: $$wt"; \
				echo "Aborting clean-all; clean or commit changes first."; \
				exit 1; \
			fi; \
		done; \
	fi

clean-all: clean clean-grafts
	rm -rf .venv .ruff_cache .mypy_cache ${WORKTREES_DIR}
	find . -type f -name '*.py[co]' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '.mypy_cache' -print0 | xargs -0 rm -rf
