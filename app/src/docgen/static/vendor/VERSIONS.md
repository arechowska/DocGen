# Vendored UI dependencies

- `htmx-2.0.8.min.js`: `htmx.org@2.0.8` from the npm registry; SHA-256 `22283ef68cb7545914f0a88a1bdedc7256a703d1d580c1d255217d0a50d31313`; Zero-Clause BSD license in `HTMX-LICENSE.txt`.
- `../css/docgen.css`: generated with `tailwindcss@3.4.17` from `../css/tailwind-source.css` and `templates/**/*.html`; SHA-256 `1af05c814d66fcd4dabe17dc0a75e01b8701fd108299ee0dd3a7cb10cdc02671`; MIT license in `TAILWIND-LICENSE.txt`.

Regenerate the stylesheet from the application directory:

```console
npx --yes tailwindcss@3.4.17 -i src/docgen/static/css/tailwind-source.css -o src/docgen/static/css/docgen.css --minify --content "src/docgen/templates/**/*.html"
```
