# hyprland-migrator

Convert legacy Hyprland `.conf` files into native Lua configuration for Hyprland
0.57. Runs on Python 3.10+ with no runtime dependencies.

## Use

From this directory:

```sh
# Preview your config on stdout; diagnostics go to stderr.
./hyprland-migrator

# Convert a specific entry point and everything it sources.
./hyprland-migrator ~/.config/hypr/hyprland.conf \
  --output ./hyprland.lua --report ./migration-report.json

# Check translation coverage without writing files.
./hyprland-migrator --check
```

The default input is `$XDG_CONFIG_HOME/hypr/hyprland.conf`, falling back to
`~/.config/hypr/hyprland.conf`. `python3 -m hyprland_migrator` works too.
Optionally install the command with `python3 -m pip install .`.

## Single-file distribution

Build an executable Python archive with the standard library:

```sh
python3 scripts/build-zipapp.py
```

Copy only `dist/hyprland-migrator.pyz` to the other computer. It contains the
converter and its option data; no installation, extraction, or pip packages are
needed. The destination still needs **Python 3.10+**.

```sh
python3 hyprland-migrator.pyz ~/.config/hypr/hyprland.conf -o ./hyprland.lua

# Or execute directly on Linux (restore the executable bit after copying).
chmod +x hyprland-migrator.pyz
./hyprland-migrator.pyz --check
```

The archive can also be renamed to `hyprland-migrator`. The original launcher in
the project root still needs the accompanying package directory. Use the built
archive for single-file distribution. Lua and Hyprland are optional validation
tools and are not bundled.

## Validate and activate

Review the Lua and report, then validate with your installed Hyprland:

```sh
luac -p ./hyprland.lua
Hyprland --verify-config -c ./hyprland.lua
```

To create the active config, run the tool with
`--output ~/.config/hypr/hyprland.lua`. Existing Lua files are protected: replacing
one requires `--force`, which saves a timestamped `.bak.*` file first. Keep your
original `.conf` files for rollback. Restart Hyprland to switch a session that
started with a legacy config to Lua. An explicit `Hyprland -c` launch argument or
session wrapper must also point at the new Lua file.

## Conversion behavior

- Nested settings become typed `hl.config()` calls. Booleans, numbers, colors,
  gradients, vectors, and CSS gaps retain their types.
- Custom variables are expanded in parse order, including reassignment. Environment
  references are evaluated through `os.getenv()` when Lua loads, rather than baking
  this machine's environment into the output. Unset references retain their text.
- `source` files are expanded into one Lua file at their original position.
  Relative paths resolve from the including file; globs are sorted. Repeated
  includes are retained, cycles and missing files are reported. Regenerate after
  changing any original source file; runtime dependencies such as scripts and
  shader files still need to exist.
- Common keyboard/mouse bindings use native `hl.dsp.*` dispatchers; flags and
  descriptions are preserved. Resize submaps use `hl.define_submap()`.
- `exec-once` and `exec-shutdown` register lifecycle callbacks. Plain `exec` runs
  on each config load. The migrator itself never executes these commands.
- Monitor definitions, disabled outputs, reserved areas, per-device blocks,
  bezier curves, animations, environment variables, and permissions are supported.
- `envd` uses `hl.env(name, value, true)` to preserve D-Bus/systemd export.
  Trackpad `gesture`/`gesturep` definitions become `hl.gesture()` calls, including
  modifiers, scaling, action modes, special workspaces, dispatcher callbacks,
  and unsetting previously defined gestures.
- Common window, layer, and workspace rules are translated, including older
  `windowrulev2` selectors. Unknown selectors and effects fail explicitly.
- Selected renamed settings move to their new categories. A standalone
  `hyprctl dispatch ...` command can become a native dispatcher, including
  `splitratio exact 1` → `hl.dsp.layout("splitratio 1 exact")`.
- Comments and directive order are preserved. Generated Lua includes source paths
  and diagnostics for review.

The converter is conservative, not a full hyprlang interpreter. Unsupported
constructs produce diagnostics with the original file and line. These include
plugins, arithmetic expressions, conditional `# hyprlang` directives,
existing Lua sources, rule blocks, complex exec window
rules, unrecognized dispatchers, and embedded legacy `hyprctl` commands in shell
scripts. External scripts are not rewritten. Removed settings are flagged for
review instead of silently dropped. Option ranges and compositor-specific
semantics still require Hyprland's verifier and a session check.

The bundled option type map was extracted from Hyprland 0.56.2's installed Lua
stubs, with selected 0.57 additions and category moves. Unknown options are
reported; this is not a claim of exhaustive support for every 0.57 feature.

## Output safety and exit codes

Original input files are never overwritten, including through symlinks or hard
links. Output is written atomically, with private file permissions. Symlink
destinations are refused. `--force` backs up existing output/report files before
replacement. Output directories must already exist.

By default, any conversion error suppresses Lua output. `--report PATH` can still
write the diagnostics. `--allow-partial` emits a review draft with `TODO` comments
and still exits with status 1; do not install it without resolving those comments.
Avoid shell redirection into an active config: shells truncate the destination
before running the converter. Use `--output` for its overwrite protection.

`--dry-run` prints the preview even when `--output` is supplied, and writes
neither output nor report. `--check` prints diagnostics only and writes nothing.

| Exit | Meaning |
| --- | --- |
| 0 | Every parsed directive was translated; validate with Hyprland before use |
| 1 | Manual migration is required |
| 2 | Input/output or command-line error |

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests cover source ordering and cycles, variables, escaping, deferred startup,
bind/submap behavior against a recording Lua API, typed values, unsupported
constructs, CLI modes, overwrite protection, backup contents, and concurrent
destination creation. Lua execution tests run when `lua` is available. A real
`Hyprland --verify-config` integration test runs when Hyprland is installed.

## API references

- [Hyprland v0.57 example config](https://github.com/hyprwm/Hyprland/blob/v0.57.0/example/hyprland.lua)
- [Configuration options](https://wiki.hypr.land/configuring/core/config-options/)
- [Dispatchers](https://wiki.hypr.land/configuring/core/dispatchers/)
- [Gestures](https://wiki.hypr.land/configuring/core/binds/gestures/)
- [Environment binding implementation](https://github.com/hyprwm/Hyprland/blob/v0.56.2/src/config/lua/bindings/LuaBindingsConfigRules.cpp#L446)
- [Submaps](https://wiki.hypr.land/configuring/core/binds/submaps/)
- [Dwindle layout messages](https://wiki.hypr.land/configuring/layouts/dwindle-layout/)

The local `output/` directory is ignored by version control so personal configs
and migration reports stay out of the distributable tool.
