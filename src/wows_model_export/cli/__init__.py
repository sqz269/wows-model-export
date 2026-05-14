"""Layer 5 -- argparse wrappers around `compose` entries.

Each submodule defines a ``main(argv: list[str] | None = None) -> int``
invoked via the ``wows-*`` entry points declared in
``pyproject.toml``. CLIs translate argv -> composer kwargs, route
``on_event`` to a printer (plaintext or ``--json-events``), and exit
with a code from :mod:`wows_model_export.cli._args` (``EXIT_OK`` = 0,
``EXIT_STEP_ERROR`` = 1, ``EXIT_CONFIG_ERROR`` = 2, ``EXIT_UNEXPECTED``
= 3).

CLI modules add no logic of their own -- that lives in
:mod:`wows_model_export.compose`. If you find yourself reaching for a
helper here that isn't argparse plumbing, push it down a layer.

Available entry points (also listed in ``pyproject.toml``):

  * ``wows-ingest-ship``           -> :mod:`.ingest_ship`
  * ``wows-scaffold-ship``         -> :mod:`.scaffold_ship`
  * ``wows-build-accessory-library`` -> :mod:`.build_accessory_library`
  * ``wows-ingest-skin-pack``      -> :mod:`.ingest_skin_pack`
  * ``wows-turret-autorig``        -> :mod:`.turret_autorig`
  * ``wows-skel-ext-resolve``      -> :mod:`.skel_ext_resolve`
  * ``wows-accessories-scan``      -> :mod:`.accessories_scan`
  * ``wows-find-ship-variants``    -> :mod:`.find_ship_variants`
  * ``wows-teardown-ship``         -> :mod:`.teardown_ship`
  * ``wows-publish``               -> :mod:`.publish`
  * ``wows-snapshot``              -> :mod:`.snapshot`
  * ``wows-build-projectile-library`` -> :mod:`.build_projectile_library`
  * ``wows-build-decal-library``   -> :mod:`.build_decal_library`
  * ``wows-build-ammo-profiles``   -> :mod:`.build_ammo_profiles`
"""

from __future__ import annotations

__all__: list[str] = []
