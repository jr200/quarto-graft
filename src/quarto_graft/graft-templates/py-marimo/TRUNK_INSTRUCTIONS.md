## Marimo Extension Installation Required

The graft "{{ graft_name }}" uses the **marimo** Quarto extension to render interactive marimo notebooks.

To enable marimo support in your trunk, you need to install the extension:

```bash
uv run quarto add marimo-team/quarto-marimo
```

After installation:
1. The extension will be available in your `_extensions/` directory
2. Add `marimo-team/marimo` to the `filters` section in your trunk's `_quarto.yaml`
3. Rebuild your site using `make preview` to see the marimo notebooks from this graft

For more information about the marimo extension, visit:
https://github.com/marimo-team/quarto-marimo
