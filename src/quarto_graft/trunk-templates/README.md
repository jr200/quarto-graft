# trunk-templates

This folder contains templates for the main trunk (documentation site) project.

When initializing a new quarto-graft project, you can choose a trunk template:

```bash
uv run trunk-init --template default
```

This will copy the trunk template into the `docs/` folder, creating the foundation for your main documentation site.

## Available Templates

- `default`: A complete Quarto website with navigation, search, and graft support

## Add-ons (--with flag)

You can include additional files using the `--with` flag:

```bash
uv run trunk-init --template default --with gh-pages
```

Available add-ons are in the [`with-addons/`](with-addons/) directory. See [`with-addons/README.md`](with-addons/README.md) for details.
