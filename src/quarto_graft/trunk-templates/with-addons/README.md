# Trunk Template Add-ons

This directory contains optional add-on templates that can be included when initializing a trunk using the `--with` flag.

## Usage

```bash
uv run trunk-init --template default --with gh-pages
```

You can use multiple `--with` flags:

```bash
uv run trunk-init --template default --with gh-pages --with analytics
```

## Available Add-ons

### gh-pages

Adds GitHub Pages specific files:
- `.nojekyll`: Tells GitHub Pages not to use Jekyll processing
- `LICENSE`: Copy of the project license for the published site

## Creating New Add-ons

To create a new add-on:

1. Create a directory under `trunk-templates/with-addons/` (e.g., `trunk-templates/with-addons/my-addon/`)
2. Add the files you want to include in the trunk when this add-on is used
3. Files will be copied to the root of the `docs/` directory

The files in add-ons are copied after the base template, so they can override template files if needed.
