# {{ graft_name }}

This graft ships a minimal Python + Quarto starter:

- uv-managed environment
- Python package at `src/{{ package_name }}/`
- Quarto docs with a sample notebook that imports the package
- Make targets to render/preview/clean

Tweak the package and docs, then run `make render` to build the section.
