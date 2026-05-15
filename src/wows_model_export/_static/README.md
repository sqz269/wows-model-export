# `_static/` — bundled webview build

The contents of `_static/webview/` are a verbatim mirror of
`webview/dist/`, the production build output from
`cd webview && npm run build`. They are committed to git (rather than
generated at install time) so:

  1. `pip install` from a wheel works without a Node toolchain.
  2. Editable installs (`pip install -e .`) still see the same files
     under the same package path — no separate path resolution per
     install mode.
  3. PyInstaller picks them up automatically as package data; the
     `_static/` directory survives `--onefile` packaging without a
     custom `--add-data` flag, because setuptools already declares
     it as `package_data` in `pyproject.toml`.

## When to refresh

Run after any change to `webview/src/**` that you want to ship in the
wheel:

```bash
cd webview && npm run build
# Sync the mirror — overwrite, never delete-then-copy (preserves git
# stat tracking for renamed files):
rm -rf src/wows_model_export/_static/webview
cp -r webview/dist src/wows_model_export/_static/webview
git add src/wows_model_export/_static/webview
```

The mirror is intentionally a flat copy; no transforms. Vite's
fingerprinted asset names (`index-<hash>.js`) handle cache-busting on
their own, and the `index.html` references them with absolute paths
(`/assets/...`) which work as-is when StaticFiles is mounted at `/`.

## Why not auto-build during install?

Considered and rejected:

  - Installing from a wheel must not require Node.
  - A custom `setuptools` build hook that shells out to `npm` would
    fail on user machines without npm — and pulling Node into the
    Python build env via a build-time dependency would balloon install
    times for users who never touch the webview.
  - The dist is small (~5 MB) and rebuilds rarely, so committing the
    mirror is the simplest reliable strategy. The duplication is a
    deliberate trade for install-time simplicity.
