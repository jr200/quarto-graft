# Makefile for a Markdown-only graft

# quarto project root - i.e., location of _quarto.yaml
PROJECT_ROOT := .

.PHONY: all render preview clean

all: render

render:
	@echo "Rendering Quarto project in $(PROJECT_ROOT)/..."
	quarto render "$(PROJECT_ROOT)" --no-execute

preview:
	@echo "Starting Quarto preview for $(PROJECT_ROOT)/..."
	quarto preview "$(PROJECT_ROOT)"

clean:
	@echo "Cleaning Quarto build artifacts..."
	rm -rf "$(PROJECT_ROOT)/_site" "$(PROJECT_ROOT)/.quarto"
