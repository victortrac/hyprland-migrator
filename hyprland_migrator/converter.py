"""Ordered, non-executing translation of hyprlang into the hl Lua API.

Includes are expanded at their original position. This preserves variable scope,
duplicate overrides, submaps, and rule order without leaving .conf dependencies.
"""

from dataclasses import asdict, dataclass
import glob
from importlib.resources import files
import json
import os
from pathlib import Path
import re


OPTIONS = json.loads(files(__package__).joinpath("options.json").read_text(encoding="utf-8"))
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}|\$([A-Za-z_][A-Za-z_0-9]*)")
DIRECTIONS = dict(l="left", r="right", u="up", d="down", t="up", b="down")
FLAGS = dict(l="locked", r="release", e="repeating", n="non_consuming",
             m="mouse", t="transparent", i="ignore_mods", s="submap_universal",
             p="dont_inhibit", o="long_press", c="click", g="drag", a="auto_consuming")
RENAMES = {
    "misc.vfr": "debug.vfr",
    "misc.layers_hog_keyboard_focus": "layers.layers_hog_keyboard_focus",
    "misc.allow_new_top_layers_over_existing_fullscreen": "layers.allow_new_top_layers_over_existing_fullscreen",
    "resize_on_border": "general.resize_on_border",
    "hover_icon_on_border": "general.hover_icon_on_border",
    "extend_border_grab_area": "general.extend_border_grab_area",
}
REMOVED = {
    "dwindle.pseudotile": "removed upstream; individual pseudo bindings remain supported",
    "decoration.shadow.ignore_window": "removed upstream; this behavior is always enabled",
    "render.cm_fs_passthrough": "removed upstream; review render.cm_auto_hdr",
}
WINDOW_BOOLS = set("center float fullscreen maximize no_initial_focus pin pseudo tile allows_input confine_pointer dim_around decorate focus_on_activate immediate keep_aspect_ratio nearest_neighbor no_anim no_auto_hdr no_blur no_dim no_focus no_follow_mouse no_glow no_max_size no_screen_share no_shadow no_shortcuts_inhibit no_vrr no_wobble no_xdg_drags opaque force_rgbx persistent_size render_unfocused stay_focused".split())
WINDOW_KINDS = {**dict.fromkeys(WINDOW_BOOLS, "bool"),
                **dict.fromkeys("content fullscreen_state group monitor move size suppress_event workspace animation idle_inhibit max_size min_size opacity".split(), "string"),
                **dict.fromkeys("no_close_for border_size rounding".split(), "int"),
                **dict.fromkeys("scrolling_width rounding_power scroll_mouse scroll_touchpad".split(), "float"),
                "border_color": "gradient"}
LAYER_KINDS = {**dict.fromkeys("blur blur_popups dim_around no_anim no_screen_share xray".split(), "bool"),
               "above_lock": "int", "animation": "string", "ignore_alpha": "float", "order": "int"}
RULE_ALIASES = dict(noblur="no_blur", noanim="no_anim", nofocus="no_focus", noinitialfocus="no_initial_focus",
                    nofollowmouse="no_follow_mouse", noshadow="no_shadow", noshortcutsinhibit="no_shortcuts_inhibit",
                    noscreenshare="no_screen_share", noborder="no_border", dimaround="dim_around", bordersize="border_size",
                    bordercolor="border_color", suppressevent="suppress_event", idleinhibit="idle_inhibit",
                    keepaspectratio="keep_aspect_ratio", forcergbx="force_rgbx", forceinput="allows_input",
                    roundingpower="rounding_power", stayfocused="stay_focused", minsize="min_size", maxsize="max_size",
                    ignorealpha="ignore_alpha", ignorezero="ignore_alpha", abovelock="above_lock", blurpopups="blur_popups")
MATCH_KINDS = {**dict.fromkeys("class title initial_class initial_title tag workspace content xdg_tag".split(), "string"),
               **dict.fromkeys("float pin focus fullscreen xwayland modal group".split(), "bool"),
               "fullscreen_state_client": "int", "fullscreen_state_internal": "int"}
MATCH_ALIASES = dict(initialClass="initial_class", initialTitle="initial_title", floating="float", pinned="pin")


class Unsupported(ValueError):
    pass


class Expr(str):
    """Already serialized Lua expression."""


def quote(value: str) -> str:
    # JSON's \uXXXX control escapes are not portable Lua string escapes.
    escapes = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return '"' + "".join(escapes.get(c, f"\\{ord(c):03d}" if ord(c) < 32 or ord(c) == 127 else c)
                         for c in value) + '"'


def lua(value) -> str:
    if isinstance(value, Expr):
        return str(value)
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"[{quote(k)}] = {lua(v)}" for k, v in value.items()) + " }"
    if isinstance(value, (tuple, list)):
        return "{ " + ", ".join(lua(v) for v in value) + " }"
    raise TypeError(value)


def call(name, *args):
    return Expr(f"hl.{name}(" + ", ".join(lua(arg) for arg in args) + ")")


def boolean(value):
    if value.lower() in {"true", "yes", "on", "1"}:
        return True
    if value.lower() in {"false", "no", "off", "0"}:
        return False
    raise Unsupported(f"expected boolean, got {value!r}")


def numeric(value, integer=False):
    if not NUMBER.fullmatch(value):
        raise Unsupported(f"expected {'integer' if integer else 'number'}, got {value!r}")
    if integer:
        if not re.fullmatch(r"[+-]?\d+", value):
            raise Unsupported(f"expected integer, got {value!r}")
        return int(value)
    # Normalize leading zeros / plus signs, which have different Lua semantics.
    n = float(value)
    if not (-float("inf") < n < float("inf")):
        raise Unsupported("non-finite number")
    return n


def string_expr(value):
    """Expand environment references at config load time, never on this host."""
    pieces, last = [], 0
    for match in VARIABLE.finditer(value):
        if match.start() > last:
            pieces.append(quote(value[last:match.start()]))
        pieces.append(f"(os.getenv({quote(match[1] or match[2])}) or {quote(match[0])})")
        last = match.end()
    if not pieces:
        return value
    if last < len(value):
        pieces.append(quote(value[last:]))
    return Expr(" .. ".join(pieces))


def typed(value, kind):
    if kind == "string":
        return string_expr(value)
    if kind == "bool":
        return boolean(value)
    if kind in {"int", "float"}:
        return numeric(value, kind == "int")
    if kind == "fontweight":
        return int(value) if re.fullmatch(r"\d+", value) else value
    if kind == "vec2":
        parts = value.split()
        if len(parts) != 2:
            raise Unsupported("expected two vector components")
        return [numeric(p) for p in parts]
    if kind == "gaps":
        parts = value.split()
        if not 1 <= len(parts) <= 4:
            raise Unsupported("expected one to four gap sizes")
        values = [numeric(p, True) for p in parts]
        if len(values) == 1:
            return values[0]
        t, r = values[:2]
        b = values[2] if len(values) >= 3 else t
        l = values[3] if len(values) == 4 else r
        return dict(top=t, right=r, bottom=b, left=l)
    if kind == "gradient":
        parts = re.findall(r"(?:rgba?\([^)]*\)|0x[0-9a-fA-F]+|[+-]?[\d.]+deg)", value)
        if not parts or re.sub(r"\s+", "", "".join(parts)) != re.sub(r"\s+", "", value):
            raise Unsupported(f"unrecognized color/gradient {value!r}")
        angle = 0
        if parts[-1].endswith("deg"):
            angle = numeric(parts.pop()[:-3])
        if not parts:
            raise Unsupported("gradient has no colors")
        # Preserve a single color as a color string, including legacy 0xAARRGGBB.
        if len(parts) == 1 and angle == 0:
            return parts[0]
        return {"colors": parts, "angle": angle}
    raise Unsupported(f"unsupported value type {kind}")


def strip_comment(line):
    """Hyprlang uses ## for a literal # (also inside quoted shell strings)."""
    if line.lstrip().startswith("#"):
        return "", line.lstrip()[1:].strip()
    out, i = [], 0
    while i < len(line):
        if line[i:i + 2] == "##":
            out.append("#")
            i += 2
        elif line[i] == "#":
            return "".join(out).strip(), line[i + 1:].strip()
        else:
            out.append(line[i])
            i += 1
    return "".join(out).strip(), ""


def fields(value, count=None):
    return [p.strip() for p in value.split(",", -1 if count is None else count - 1)]


def chord(mods, key):
    if not key or "$" in mods or "$" in key:
        raise Unsupported("empty key or unresolved variable in keybind")
    return " + ".join([*mods.split(), key])


def direction(value):
    if value not in DIRECTIONS and value not in DIRECTIONS.values():
        raise Unsupported(f"unsupported direction {value!r}")
    return DIRECTIONS.get(value, value)


def layout_message(name, arg):
    # The old splitratio dispatcher used 'exact 1'; layout messages use '1 exact'.
    if name == "splitratio" and arg.startswith("exact "):
        arg = arg[6:] + " exact"
    return name + (" " + arg if arg else "")


def dispatcher(name, arg, mouse=False):
    if name in {"exec", "execr"}:
        if arg.startswith("["):
            raise Unsupported("exec window rules need a native rules table; convert manually")
        # Migrate a standalone hyprctl call without attempting to parse shell programs.
        match = re.fullmatch(r"hyprctl\s+dispatch\s+([a-z]+)(?:\s+(.*))?", arg)
        if match and not re.search(r"[;&|`$()\n\\\"']", match[2] or ""):
            return dispatcher(match[1], (match[2] or "").strip())
        if re.search(r"\bhyprctl\b.*\b(dispatch|keyword)\b", arg):
            raise Unsupported("embedded hyprctl dispatch/keyword command needs manual Lua API migration")
        return call("dsp.exec_raw" if name == "execr" else "dsp.exec_cmd", string_expr(arg))
    noarg = {"killactive": "window.close", "forcekillactive": "window.kill",
             "exit": "exit", "togglegroup": "group.toggle", "toggleSwallow": "window.toggle_swallow",
             "bringactivetotop": "window.bring_to_top"}
    if name in noarg:
        if arg:
            raise Unsupported(f"unexpected argument for {name}")
        return call("dsp." + noarg[name])
    if name in {"togglefloating", "setfloating", "settiled", "pin", "pseudo", "closewindow", "killwindow"}:
        method = {"togglefloating": "float", "setfloating": "float", "settiled": "float",
                  "closewindow": "close", "killwindow": "kill"}.get(name, name)
        spec = {}
        if arg and arg != "active":
            spec["window"] = arg
        if name in {"setfloating", "settiled"}:
            spec["action"] = "enable" if name == "setfloating" else "disable"
        return call("dsp.window." + method, spec) if spec else call("dsp.window." + method)
    if name == "fullscreen":
        parts = arg.split()
        if len(parts) > 2 or (parts and parts[0] not in {"0", "1"}) or (len(parts) == 2 and parts[1] not in {"toggle", "set", "unset"}):
            raise Unsupported("unrecognized fullscreen arguments")
        return call("dsp.window.fullscreen", {"mode": "maximized" if parts and parts[0] == "1" else "fullscreen",
                                              "action": parts[1] if len(parts) == 2 else "toggle"})
    if name in {"workspace", "focusworkspaceoncurrentmonitor", "focusmonitor", "focuswindow", "movefocus"}:
        key = {"workspace": "workspace", "focusworkspaceoncurrentmonitor": "workspace",
               "focusmonitor": "monitor", "focuswindow": "window", "movefocus": "direction"}[name]
        if not arg:
            raise Unsupported(f"missing argument for {name}")
        spec = {key: direction(arg) if name == "movefocus" else arg}
        if name == "focusworkspaceoncurrentmonitor":
            spec["on_current_monitor"] = True
        return call("dsp.focus", spec)
    if name in {"movetoworkspace", "movetoworkspacesilent"}:
        parts = fields(arg, 2)
        if not parts[0]:
            raise Unsupported("missing workspace")
        spec = {"workspace": parts[0], "follow": name == "movetoworkspace"}
        if len(parts) == 2:
            spec["window"] = parts[1]
        return call("dsp.window.move", spec)
    if name in {"movewindow", "resizewindow"} and mouse:
        if arg:
            raise Unsupported("mouse drag/resize arguments need manual review")
        return call("dsp.window.drag" if name == "movewindow" else "dsp.window.resize")
    if name in {"movewindow", "swapwindow"}:
        return call("dsp.window.move" if name == "movewindow" else "dsp.window.swap", {"direction": direction(arg)})
    if name in {"resizeactive", "moveactive"}:
        parts = arg.split()
        exact = bool(parts and parts[0] == "exact")
        if exact:
            parts.pop(0)
        if len(parts) != 2:
            raise Unsupported("expected two resize/move coordinates")
        return call("dsp.window.resize" if name == "resizeactive" else "dsp.window.move",
                    {"x": numeric(parts[0]), "y": numeric(parts[1]), "relative": not exact})
    if name in {"togglesplit", "swapsplit", "splitratio", "layoutmsg"}:
        return call("dsp.layout", arg if name == "layoutmsg" else layout_message(name, arg))
    if name in {"submap", "togglespecialworkspace", "global", "sendshortcut", "pass", "event"}:
        if name == "sendshortcut":
            parts = fields(arg)
            if len(parts) not in {2, 3}:
                raise Unsupported("expected modifiers, key, optional window")
            spec = {"mods": parts[0], "key": parts[1]}
            if len(parts) == 3:
                spec["window"] = parts[2]
            return call("dsp.send_shortcut", spec)
        if name == "pass":
            return call("dsp.pass", {"window": arg})
        method = "workspace.toggle_special" if name == "togglespecialworkspace" else name
        return call("dsp." + method, arg)
    if name in {"cyclenext", "focuscurrentorlast", "focusurgentorlast"}:
        if arg:
            raise Unsupported(f"{name} arguments require manual review")
        if name == "cyclenext":
            return call("dsp.window.cycle_next")
        return call("dsp.focus", {"last" if name == "focuscurrentorlast" else "urgent_or_last": True})
    raise Unsupported(f"dispatcher {name!r} is not supported")


def gesture(value, flags=""):
    """Translate legacy gesture[p] selectors, options and action arguments."""
    if set(flags) - {"p"}:
        raise Unsupported(f"unsupported gesture flag(s): {flags!r}")
    parts = fields(value)
    if len(parts) < 3:
        raise Unsupported("gesture needs fingers, direction[, mod: ...][, scale: ...], action")
    fingers = numeric(parts[0], True)
    if not 2 <= fingers <= 9:
        raise Unsupported("gesture finger count must be between 2 and 9")
    if parts[1] not in {"swipe", "horizontal", "vertical", "left", "right", "up", "down", "pinch", "pinchin", "pinchout"}:
        raise Unsupported(f"unsupported gesture direction {parts[1]!r}")
    spec = {"fingers": fingers, "direction": parts[1]}
    if flags:
        spec["disable_inhibit"] = True
    index = 2
    while index < len(parts):
        name, sep, val = parts[index].partition(":")
        if not sep:
            break
        if name == "mod":
            if "$" in val:
                raise Unsupported("unresolved gesture modifier variable")
            spec["mods"] = val.strip()
        elif name == "scale":
            # Legacy Hyprland clamps the multiplier to this range.
            spec["scale"] = max(0.1, min(10.0, numeric(val.strip())))
        else:
            raise Unsupported(f"unsupported gesture option {name!r}")
        index += 1
    if index == len(parts) or not parts[index]:
        raise Unsupported("gesture is missing an action")
    action = {"cursorZoom": "cursor_zoom", "scrollMove": "scroll_move"}.get(parts[index], parts[index])
    args = parts[index + 1:]
    while args and not args[-1]:
        args.pop()
    spec["action"] = action
    if action == "dispatcher":
        if not args or not args[0]:
            raise Unsupported("dispatcher gesture needs a dispatcher name")
        action_call = dispatcher(args[0], ",".join(args[1:]))
        spec["action"] = Expr(f"function() hl.dispatch({action_call}) end")
    elif action == "special":
        if len(args) > 1:
            raise Unsupported("special gesture takes one workspace name")
        spec["workspace_name"] = args[0] if args else ""
    elif action in {"float", "fullscreen"}:
        modes = {"toggle", "float", "tile"} if action == "float" else {"fullscreen", "maximize"}
        if len(args) > 1 or (args and args[0] not in modes):
            raise Unsupported(f"unsupported {action} gesture mode")
        if args:
            spec["mode"] = args[0]
    elif action == "cursor_zoom":
        if not 1 <= len(args) <= 2:
            raise Unsupported("cursorZoom gesture needs zoom level[, mult|live]")
        spec["zoom_level"] = numeric(args[0])
        if len(args) == 2:
            if args[1] not in {"toggle", "mult", "live"}:
                raise Unsupported(f"unsupported cursor zoom mode {args[1]!r}")
            spec["mode"] = args[1]
    elif action in {"workspace", "move", "resize", "close", "scroll_move", "unset"}:
        if args:
            raise Unsupported(f"unexpected arguments for {action} gesture")
    else:
        raise Unsupported(f"unsupported gesture action {action!r}")
    return spec


@dataclass
class Diagnostic:
    path: str
    line: int
    severity: str
    message: str
    source: str


@dataclass
class Result:
    lua: str
    diagnostics: list[Diagnostic]
    inputs: list[Path]
    converted: int

    @property
    def errors(self):
        return [d for d in self.diagnostics if d.severity == "error"]

    def report(self):
        return {"target": "Hyprland 0.57", "converted": self.converted,
                "inputs": [str(p) for p in self.inputs],
                "errors": len(self.errors), "diagnostics": [asdict(d) for d in self.diagnostics]}


class Converter:
    def __init__(self):
        self.variables = {}
        self.environment = dict(os.environ)
        self.lines = ["-- Generated by hyprland-migrator for Hyprland 0.57.",
                      "-- Original .conf files were left untouched.", ""]
        self.diagnostics = []
        self.inputs = []
        self.stack = []
        self.sections = []
        self.device = None
        self.submap = None
        self.converted = 0

    def emit(self, line=""):
        if line.lstrip().startswith("--"):
            # Source paths can themselves contain newlines. Keep them in comments.
            line = line.replace("\r", "\\r").replace("\n", "\n-- ")
        self.lines.append(("    " if self.submap else "") + line)

    def diagnostic(self, path, line, source, message, severity="error"):
        self.diagnostics.append(Diagnostic(str(path), line, severity, message, source))
        self.emit(f"-- {'TODO' if severity == 'error' else 'NOTE'} {path}:{line}: {message}")
        if severity == "error":
            self.emit("-- Legacy: " + source)

    def expand(self, value):
        # Resolve custom variables now so delayed callbacks retain the value that
        # was in effect on the original line. Leave environment expansion for Lua/shell.
        for _ in range(50):
            new = value
            for key in sorted(self.variables, key=len, reverse=True):
                new = new.replace("${" + key + "}", self.variables[key])
                new = new.replace("$" + key, self.variables[key])
            if new == value:
                return value
            value = new
        raise Unsupported("recursive variable expansion")

    def convert(self, path):
        self.read(Path(path).expanduser().absolute())
        if self.sections:
            self.diagnostic(path, 0, "", "unclosed configuration section")
        if self.submap:
            self.submap = None
            self.emit("end)")
        return Result("\n".join(self.lines) + "\n", self.diagnostics, self.inputs, self.converted)

    def read(self, path):
        resolved = path.resolve()
        if resolved in self.stack:
            raise Unsupported("source cycle: " + " -> ".join(str(p) for p in [*self.stack, resolved]))
        if len(self.stack) >= 64:
            raise Unsupported("source nesting exceeds 64 files")
        try:
            contents = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            raise Unsupported(f"cannot read {path}: {error}") from error
        if resolved not in self.inputs:
            self.inputs.append(resolved)
        self.stack.append(resolved)
        self.emit(f"-- Source: {path}")
        pending = ""
        start = 1
        for lineno, raw in enumerate(contents.splitlines(), 1):
            if not pending:
                start = lineno
            if raw.endswith("\\"):
                pending += raw[:-1].rstrip(" \t")
                continue
            raw = pending + raw
            pending = ""
            code, comment = strip_comment(raw)
            if comment.startswith("hyprlang"):
                self.diagnostic(path, start, raw.strip(), "hyprlang comment directives require manual migration")
            if not code:
                self.emit("-- " + comment if comment else "")
                continue
            try:
                self.process(path, start, code)
                if comment:
                    self.emit("-- " + comment)
            except (Unsupported, ValueError) as error:
                self.diagnostic(path, start, raw.strip(), str(error))
        if pending:
            self.diagnostic(path, start, pending, "unfinished line continuation")
        self.stack.pop()

    def process(self, path, lineno, code):
        if code == "}":
            if not self.sections:
                raise Unsupported("unexpected closing brace")
            closed = self.sections.pop()
            if closed == "device" and not self.sections and self.device is not None:
                spec, self.device = self.device, None
                if not spec.get("name"):
                    raise Unsupported("device section needs a name")
                self.emit(str(call("device", spec)))
            return
        match = re.fullmatch(r"([\w.:-]+)\s*\{", code)
        if match:
            self.sections.append(match[1])
            if self.sections == ["device"]:
                self.device = {}
            return
        if "=" not in code:
            raise Unsupported("expected key = value or a section brace on its own line")
        key, value = (part.strip() for part in code.split("=", 1))
        if key.startswith("$"):
            if not re.fullmatch(r"\$[A-Za-z_][A-Za-z_0-9]*", key):
                raise Unsupported("unsupported variable name")
            expanded = self.expand(value)
            if re.search(r"\$\{?" + re.escape(key[1:]) + r"\b", expanded):
                raise Unsupported("self-referencing variable")
            self.variables[key[1:]] = expanded
            self.emit(f"-- {key} = {expanded}")
            self.converted += 1
            return
        value = self.expand(value)
        if "{{" in value or "}}" in value:
            raise Unsupported("hyprlang expressions require manual migration")
        if key == "source":
            expanded = VARIABLE.sub(lambda m: self.environment.get(m[1] or m[2], m[0]), value)
            if "$" in expanded:
                raise Unsupported(f"unresolved source path {expanded!r}")
            source = Path(expanded).expanduser()
            if not source.is_absolute():
                source = path.parent / source
            matches = sorted(glob.glob(str(source)))
            if not matches:
                raise Unsupported(f"source matched no files: {source}")
            for name in matches:
                if Path(name).suffix == ".lua":
                    raise Unsupported("existing Lua sources must be included manually")
                self.read(Path(name))
            self.converted += 1
            return
        if key == "submap":
            if "$" in value:
                raise Unsupported("unresolved submap variable")
            if self.submap:
                self.submap = None
                self.emit("end)")
            if value != "reset":
                self.emit(f"hl.define_submap({quote(value)}, function()")
                self.submap = value
            self.converted += 1
            return
        if key.startswith("bind") and key != "binds":
            self.bind(key[4:], value)
        elif key == "unbind":
            parts = fields(value)
            if len(parts) != 2:
                raise Unsupported("expected modifiers, key for unbind")
            self.emit(str(call("unbind", chord(*parts))))
        elif key in {"exec", "exec-once", "exec-shutdown"}:
            action = dispatcher("exec", value)
            # hl.exec_cmd executes, whereas hl.dsp.* describes a deferred action.
            action = call("dispatch", action)
            event = {"exec-once": "hyprland.start", "exec-shutdown": "hyprland.shutdown"}.get(key)
            self.emit(f"hl.on({quote(event)}, function() {action} end)" if event else str(action))
        elif key in {"env", "envd"}:
            parts = fields(value, 2)
            if len(parts) != 2 or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", parts[0]):
                raise Unsupported("expected environment variable name, value")
            self.environment[parts[0]] = VARIABLE.sub(lambda m: self.environment.get(m[1] or m[2], m[0]), parts[1])
            args = [parts[0], string_expr(parts[1])]
            if key == "envd":
                args.append(True)
            self.emit(str(call("env", *args)))
        elif key.startswith("gesture") and key != "gestures":
            self.emit(str(call("gesture", gesture(value, key[7:]))))
        elif key == "monitor":
            self.emit(str(call("monitor", self.monitor(value))))
        elif key == "bezier":
            parts = fields(value)
            if len(parts) != 5:
                raise Unsupported("bezier needs a name and four coordinates")
            coords = [numeric(p) for p in parts[1:]]
            self.emit(str(call("curve", parts[0], {"type": "bezier", "points": [coords[:2], coords[2:]]})))
        elif key == "animation":
            parts = fields(value, 5)
            if len(parts) < 4:
                raise Unsupported("animation needs leaf, enabled, speed, curve[, style]")
            spec = {"leaf": parts[0], "enabled": boolean(parts[1]), "speed": numeric(parts[2]), "bezier": parts[3]}
            if len(parts) == 5:
                spec["style"] = parts[4]
            self.emit(str(call("animation", spec)))
        elif key == "permission":
            parts = fields(value)
            if len(parts) != 3:
                raise Unsupported("permission needs binary, type, mode")
            self.emit(str(call("permission", *parts)))
        elif key in {"windowrule", "windowrulev2", "layerrule", "workspace"}:
            self.rule(key, value)
        else:
            self.option(path, lineno, code, key, value)
        self.converted += 1

    def bind(self, flags, value):
        unknown = set(flags) - set(FLAGS) - {"d"}
        if unknown:
            raise Unsupported("unsupported bind flag(s): " + ", ".join(sorted(unknown)))
        parts = fields(value, 5 if "d" in flags else 4)
        if len(parts) < (4 if "d" in flags else 3):
            raise Unsupported("bind needs modifiers, key[, description], dispatcher[, argument]")
        mods, key = parts[:2]
        opts = {FLAGS[f]: True for f in flags if f != "d"}
        if "d" in flags:
            opts["description"] = parts.pop(2)
        args = [chord(mods, key), dispatcher(parts[2], parts[3] if len(parts) == 4 else "", "m" in flags)]
        if opts:
            args.append(opts)
        self.emit(str(call("bind", *args)))

    def option(self, path, lineno, code, key, value):
        if self.sections and self.sections[0] == "device":
            if self.sections != ["device"] or self.device is None:
                raise Unsupported("nested device sections require manual migration")
            name = key.replace("-", "_")
            candidates = ["input." + name, "input.touchpad." + name, "input.tablet." + name, "input.touchdevice." + name]
            kind = next((OPTIONS[k] for k in candidates if k in OPTIONS), None)
            if name == "name":
                kind = "string"
            if kind is None:
                raise Unsupported(f"unknown device option {name!r}")
            self.device[name] = typed(value, kind)
            return
        name = re.sub(r"[:.]", ".", ".".join([*self.sections, key])).replace("-", "_")
        if name in REMOVED:
            raise Unsupported(REMOVED[name])
        original = name
        name = RENAMES.get(name, name)
        if name not in OPTIONS:
            raise Unsupported(f"unknown option {original!r}; plugin and removed options need manual migration")
        if original != name:
            self.diagnostic(path, lineno, code, f"moved {original} to {name}", "info")
        result = typed(value, OPTIONS[name])
        self.emit(str(call("config", {name: result})))

    @staticmethod
    def monitor(value):
        parts = fields(value)
        if len(parts) < 2:
            raise Unsupported("monitor needs an output and mode")
        spec = {"output": parts[0]}
        if parts[1] in {"disable", "disabled"}:
            if len(parts) != 2:
                raise Unsupported("unexpected disabled monitor options")
            return {**spec, "disabled": True}
        if parts[1] == "addreserved":
            if len(parts) != 6:
                raise Unsupported("addreserved needs top, bottom, left, right")
            t, b, l, r = [numeric(p, True) for p in parts[2:]]
            return {**spec, "reserved": dict(top=t, right=r, bottom=b, left=l)}
        if len(parts) < 4:
            raise Unsupported("monitor needs output, mode, position, scale")
        spec.update(mode=parts[1], position=parts[2], scale=parts[3])
        extras = parts[4:]
        kinds = {"mirror": "string", "bitdepth": "int", "transform": "int", "vrr": "int", "cm": "string",
                 "sdrbrightness": "float", "sdrsaturation": "float", "supports_wide_color": "int", "supports_hdr": "int",
                 "sdr_min_luminance": "float", "sdr_max_luminance": "int", "min_luminance": "float",
                 "max_luminance": "int", "max_avg_luminance": "int", "icc": "string"}
        if len(extras) % 2:
            raise Unsupported("unpaired monitor option")
        for key, val in zip(extras[::2], extras[1::2]):
            if key not in kinds:
                raise Unsupported(f"unsupported monitor option {key!r}")
            spec[key] = typed(val, kinds[key])
        return spec

    def rule(self, key, value):
        # Rules are deliberately handled only for known effects and selectors.
        if key == "workspace":
            parts = fields(value)
            spec = {"workspace": parts[0]}
            kinds = {"monitor": "string", "default": "bool", "persistent": "bool", "gapsin": "gaps", "gapsout": "gaps",
                     "bordersize": "int", "border": "bool", "rounding": "bool", "decorate": "bool",
                     "shadow": "bool", "on-created-empty": "string", "defaultName": "string", "layout": "string"}
            names = {"gapsin": "gaps_in", "gapsout": "gaps_out", "bordersize": "border_size",
                     "border": "no_border", "rounding": "no_rounding", "shadow": "no_shadow",
                     "on-created-empty": "on_created_empty", "defaultName": "default_name"}
            for part in parts[1:]:
                k, sep, val = part.partition(":")
                if not sep or k not in kinds:
                    raise Unsupported(f"unsupported workspace rule {part!r}")
                parsed = typed(val, kinds[k])
                spec[names.get(k, k)] = not parsed if k in {"border", "rounding", "shadow"} else parsed
            self.emit(str(call("workspace_rule", spec)))
            return
        layer = key == "layerrule"
        kinds = LAYER_KINDS if layer else WINDOW_KINDS
        selectors = {"namespace": "string"} if layer else MATCH_KINDS
        parts = fields(value)
        spec = {"match": {}}
        # Pre-0.53 v1 rules had exactly effect, class-regex (namespace for layers).
        # Newer forms may interleave effects and explicit selectors.
        legacy_regex = len(parts) == 2 and not any(
            parts[1].startswith(prefix + ":")
            for prefix in [*selectors, *MATCH_ALIASES, "match"])
        if legacy_regex:
            spec["match"]["namespace" if layer else "class"] = parts.pop()
        elif key == "windowrule" and not any(":" in p for p in parts):
            raise Unsupported("window rule has no selector")
        for part in parts:
            selector = part.removeprefix("match:")
            prefix, sep, val = selector.partition(":")
            prefix = MATCH_ALIASES.get(prefix, prefix)
            if sep and prefix in selectors:
                spec["match"][prefix] = typed(val, selectors[prefix])
                continue
            effect, _, arg = part.partition(" ")
            effect = RULE_ALIASES.get(effect, effect)
            if effect == "no_border" and not layer:
                if arg and not boolean(arg):
                    raise Unsupported("noborder false cannot be represented without the effective border size")
                spec["border_size"] = 0
                continue
            if effect not in kinds:
                raise Unsupported(f"unsupported {'layer' if layer else 'window'} rule effect {effect!r}")
            if effect == "ignore_alpha" and not arg:
                arg = "0"
            spec[effect] = typed(arg or "true", kinds[effect]) if kinds[effect] == "bool" else typed(arg, kinds[effect])
        if not spec["match"] or len(spec) == 1:
            raise Unsupported("rule needs both a selector and an effect")
        self.emit(str(call("layer_rule" if layer else "window_rule", spec)))
