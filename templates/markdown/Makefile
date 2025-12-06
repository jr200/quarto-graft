# Makefile for a Markdown-only graft

DOCS_DIR := docs

.PHONY: all render preview clean

all: render

render:
	@echo "Rendering Quarto project in $(DOCS_DIR)/..."
	quarto render "$(DOCS_DIR)" --no-execute

preview:
	@echo "Starting Quarto preview for $(DOCS_DIR)/..."
	quarto preview "$(DOCS_DIR)"

clean:
	@echo "Cleaning Quarto build artifacts..."
	rm -rf "$(DOCS_DIR)/_site" "$(DOCS_DIR)/.quarto"
