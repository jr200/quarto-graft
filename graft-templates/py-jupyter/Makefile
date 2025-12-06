# Makefile for a single graft branch

DOCS_DIR := docs

.PHONY: all env render preview clean

all: render

env:
	@echo "Syncing uv environment..."
	uv sync --frozen || uv sync

render: env
	@echo "Rendering Quarto project in $(DOCS_DIR)/..."
	uv run quarto render "$(DOCS_DIR)" --no-execute

preview: env
	@echo "Starting Quarto preview for $(DOCS_DIR)/..."
	uv run quarto preview "$(DOCS_DIR)"

clean:
	@echo "Cleaning Quarto build artifacts..."
	rm -rf "$(DOCS_DIR)/_site" "$(DOCS_DIR)/.quarto"
