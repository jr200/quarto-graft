# Makefile for a single graft branch

# quarto project root - i.e., location of _quarto.yaml
PROJECT_ROOT := .

.PHONY: all env render preview clean

all: render

env:
	@echo "Syncing uv environment..."
	uv sync --frozen || uv sync

render: env
	@echo "Rendering Quarto project in $(PROJECT_ROOT)/..."
	uv run quarto render "$(PROJECT_ROOT)" --no-execute

preview: env
	@echo "Starting Quarto preview for $(PROJECT_ROOT)/..."
	uv run quarto preview "$(PROJECT_ROOT)"

clean:
	@echo "Cleaning Quarto build artifacts..."
	rm -rf "$(PROJECT_ROOT)/_site" "$(PROJECT_ROOT)/.quarto"
