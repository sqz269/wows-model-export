# `_static/` — staging dir for the bundled webview

`_static/webview/` is a **build-time** mirror of `webview/dist/`, not
source. The directory itself is git-ignored (see `.gitignore`); only this
README and `.gitattributes` are tracked.

## How the bundle reaches each consumer

  1. **Local dev (editable install):** the resolver in
     `wows_model_export/server/static.py` first checks
     `_static/webview/index.html`. If absent, it walks up to find
     `webview/dist/index.html` instead. So `cd webview && npm run build`
     followed by `wows-webview-serve` Just Works — no copy required.
  2. **Wheel install (`pip install <wheel>`):** the wheel must already
     contain `_static/webview/`. The CI release workflow runs
     `npm run build` and copies `webview/dist/*` into `_static/webview/`
     before invoking `python -m build`. `pyproject.toml`'s
     `[tool.setuptools.package-data]` declares the subtree so setuptools
     pulls it into the wheel.
  3. **PyInstaller frozen exe:** the spec at `pyinstaller/wows-webview.spec`
     bundles `_static/webview/` as a data dir; the release workflow stages
     the bundle the same way before invoking PyInstaller.

## Building a wheel locally

You must populate the mirror first — setuptools silently drops missing
package_data, so a wheel built without this step ships without the UI:

```bash
cd webview && npm run build && cd ..
rm -rf src/wows_model_export/_static/webview
cp -r webview/dist src/wows_model_export/_static/webview
python -m build --wheel
```

The same dance lives in `.github/workflows/release.yml` — keep them in
sync if either changes.
