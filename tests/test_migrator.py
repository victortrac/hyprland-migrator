import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hyprland_migrator.cli import main, safe_write
from hyprland_migrator.converter import Converter, Unsupported, dispatcher, gesture, lua, typed


FIXTURE = """# Representative legacy configuration
$mainMod = SUPER
$terminal = foot
env = SSH_AUTH_SOCK,$XDG_RUNTIME_DIR/agent
env = GDK_BACKEND,wayland,x11
envd = HYPRLAND_MIGRATOR_TEST_LIST,wayland,x11
gesture = 3, horizontal, workspace
gesturep = 4, down, mod: SUPER, scale: 1.5, special, scratchpad
gesture = 3, up, fullscreen, maximize
gesture = 5, left, float, tile
gesture = 2, pinch, cursorZoom, 2.0, mult
gesture = 4, up, dispatcher, exec, foot
gesture = 4, right, scrollMove
monitor = DP-1,2560x1440@144,0x0,1,bitdepth,10
monitor = HDMI-A-1,disable
exec-once = $terminal --server
exec-shutdown = echo goodbye
general {
    border_size = 3
    gaps_out = 10 20 30 40
    col.active_border = rgba(33ccffee) rgba(00ff99ee) 45deg
}
input {
    kb_layout = us
    touchpad {
        tap-to-click = yes
    }
}
decoration {
    shadow {
        offset = 1 2
    }
}
device {
    name = test-mouse
    sensitivity = -0.5
}
animations {
    bezier = gentle,0.2,0.9,0.1,1
    animation = windows,1,7,gentle,popin 80%
}
bind = $mainMod,Return,exec,$terminal -e printf 'hello, world'
bindeld = ,XF86AudioRaiseVolume,Volume up,exec,echo up
bindm = $mainMod,mouse:272,movewindow
bind = $mainMod,1,movetoworkspace,1
bind = $mainMod,2,movetoworkspacesilent,2
bind = $mainMod,R,submap,resize
submap = resize
binde = ,h,resizeactive,-20 0
bind = ,escape,submap,reset
submap = reset
bind = $mainMod,E,exec,hyprctl dispatch splitratio exact 1
unbind = $mainMod,Q
windowrulev2 = float,class:^(pavucontrol)$
windowrulev2 = opacity 0.9 0.8,class:^(foot)$,floating:1
windowrule = size 800 600,^(dialog)$
windowrule = no_blur on,match:class:^(foot)$,match:title:^(Terminal)$
layerrule = blur,waybar
layerrule = ignorealpha 0.5,waybar
workspace = 1,monitor:DP-1,default:true,gapsout:10 20,border:false
"""


class MigratorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.write("hyprland.conf", FIXTURE)

    def write(self, path, contents):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        return target

    def convert(self, content):
        self.source.write_text(content)
        return Converter().convert(self.source)

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main([str(self.source), *(str(a) for a in args)])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_fixture(self):
        result = Converter().convert(self.source)
        self.assertEqual(result.errors, [])
        self.assertIn('hl.dsp.layout("splitratio 1 exact")', result.lua)
        self.assertIn('["follow"] = true', result.lua)
        self.assertIn('["follow"] = false', result.lua)
        self.assertIn('["description"] = "Volume up"', result.lua)

    @unittest.skipUnless(shutil.which("lua"), "Lua interpreter unavailable")
    def test_generated_lua_behavior(self):
        result = Converter().convert(self.source)
        self.assertEqual(result.errors, [])
        # Execute only against a recording API, never against a live compositor.
        harness = r'''
local calls, callbacks, submap = {}, {}, "reset"
local function record(name, ...)
    table.insert(calls, { name = name, args = {...}, submap = submap })
end
local function dsp(path)
    return setmetatable({}, {
        __index = function(_, key) return dsp(path .. "." .. key) end,
        __call = function(_, ...) return { dispatcher = path, args = {...} } end,
    })
end
hl = { dsp = dsp("hl.dsp") }
setmetatable(hl, { __index = function(_, key)
    return function(...) record(key, ...) end
end })
hl.on = function(event, fn) callbacks[event] = callbacks[event] or {}; table.insert(callbacks[event], fn) end
hl.define_submap = function(name, fn) submap = name; fn(); submap = "reset" end
'''
        checks = r'''
local binds, options = {}, {}
for _, c in ipairs(calls) do
    assert(c.name ~= "dispatch", "startup command executed at config evaluation")
    if c.name == "config" then for k,v in pairs(c.args[1]) do options[k] = v end end
    if c.name == "bind" then binds[c.args[1]] = c end
end
assert(options["input.touchpad.tap_to_click"] == true)
assert(options["general.gaps_out"].left == 40)
assert(options["general.gaps_out"].top == 10)
assert(options["general.col.active_border"].angle == 45)
assert(options["decoration.shadow.offset"][2] == 2)
assert(binds["SUPER + Return"].args[2].args[1] == "foot -e printf 'hello, world'")
assert(binds["XF86AudioRaiseVolume"].args[3].repeating == true)
assert(binds["XF86AudioRaiseVolume"].args[3].locked == true)
assert(binds["SUPER + mouse:272"].args[2].dispatcher == "hl.dsp.window.drag")
assert(binds["SUPER + mouse:272"].args[3].mouse == true)
assert(binds["h"].submap == "resize")
assert(binds["h"].args[2].args[1].relative == true)
assert(binds["h"].args[2].args[1].x == -20)
assert(binds["SUPER + E"].submap == "reset")
assert(#callbacks["hyprland.start"] == 1)
callbacks["hyprland.start"][1]()
assert(calls[#calls].name == "dispatch")
assert(calls[#calls].args[1].args[1] == "foot --server")
'''
        completed = subprocess.run(["lua", "-"], input=harness + result.lua + checks, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_source_order_variables_globs_and_overrides(self):
        self.write("parts/10.conf", "$app = foot\ngeneral:border_size = 2\nbind = SUPER,A,exec,$app\n")
        self.write("parts/20.conf", "$app = kitty\ngeneral:border_size = 4\n")
        result = self.convert("$parts = parts\nsource = $parts/*.conf\nbind = SUPER,B,exec,$app\n")
        self.assertFalse(result.errors)
        self.assertEqual(len(result.inputs), 3)
        self.assertLess(result.lua.index('= 2'), result.lua.index('= 4'))
        self.assertIn('hl.dsp.exec_cmd("foot")', result.lua)
        self.assertIn('hl.dsp.exec_cmd("kitty")', result.lua)

    def test_source_environment_does_not_mutate_host(self):
        self.write("parts/a.conf", "general:border_size = 3\n")
        before = os.environ.get("MIGRATOR_TEST_PATH")
        result = self.convert("env = MIGRATOR_TEST_PATH,parts\nsource = $MIGRATOR_TEST_PATH/*.conf\n")
        self.assertFalse(result.errors)
        self.assertEqual(os.environ.get("MIGRATOR_TEST_PATH"), before)

    def test_cycles_missing_sources_and_line_diagnostics(self):
        for content in ("source = hyprland.conf", "source = nonexistent*.conf", "bad_option = 3"):
            with self.subTest(content=content):
                result = self.convert(content)
                self.assertEqual(len(result.errors), 1)
                self.assertEqual(result.errors[0].line, 1)
                self.assertIn("TODO", result.lua)

    def test_comment_hashes_and_continuations(self):
        result = self.convert("## Heading\n# ignored\nexec-once = echo ##abc # comment\nbind = SUPER,A,exec,echo \\\n hello\n")
        self.assertFalse(result.errors)
        self.assertIn('echo #abc', result.lua)
        self.assertIn('echo hello', result.lua)
        self.assertIn('-- comment', result.lua)

    def test_environment_kept_for_runtime(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/should-not-be-baked-in"}):
            result = self.convert("env = SSH_AUTH_SOCK,$XDG_RUNTIME_DIR/gcr/ssh")
        self.assertFalse(result.errors)
        self.assertNotIn("should-not-be-baked-in", result.lua)
        self.assertIn('os.getenv("XDG_RUNTIME_DIR")', result.lua)

    def test_envd_preserves_export_flag_and_full_value(self):
        result = self.convert("envd = BACKENDS,wayland,x11\nenv = NORMAL,\nenvd = AGENT,$XDG_RUNTIME_DIR/agent\n")
        self.assertFalse(result.errors)
        self.assertIn('hl.env("BACKENDS", "wayland,x11", true)', result.lua)
        self.assertIn('hl.env("NORMAL", "")', result.lua)
        self.assertIn('or "$XDG_RUNTIME_DIR") .. "/agent", true)', result.lua)
        for content in ("envd = BAD NAME,value", "envd = MISSING_SEPARATOR", "envd = ,value"):
            with self.subTest(content=content):
                self.assertTrue(self.convert(content).errors)

    def test_gesture_selectors_modes_and_flags(self):
        self.assertEqual(gesture("3, horizontal, workspace"),
                         dict(fingers=3, direction="horizontal", action="workspace"))
        self.assertEqual(gesture("4, down, mod: SUPER SHIFT, scale: 1.5, special, scratchpad", "p"),
                         dict(fingers=4, direction="down", mods="SUPER SHIFT", scale=1.5,
                              action="special", workspace_name="scratchpad", disable_inhibit=True))
        self.assertEqual(gesture("3, up, fullscreen, maximize")["mode"], "maximize")
        self.assertEqual(gesture("3, left, float, tile")["mode"], "tile")
        self.assertEqual(gesture("2, pinchin, cursorZoom, 2.0, mult")["action"], "cursor_zoom")
        self.assertEqual(gesture("2, pinchin, cursorZoom, 2.0, mult")["zoom_level"], 2.0)
        self.assertEqual(gesture("4, right, scrollMove")["action"], "scroll_move")
        self.assertEqual(gesture("3, horizontal, scale: 20, unset")["scale"], 10.0)
        self.assertEqual(gesture("3, horizontal, scale: 0.01, unset")["scale"], 0.1)
        self.assertEqual(gesture("3, horizontal, unset")["action"], "unset")
        result = self.convert("$mods = SUPER SHIFT\n$scale = 1.5\ngesturep = 4, down, mod: $mods, scale: $scale, close\n")
        self.assertFalse(result.errors)
        self.assertIn('["mods"] = "SUPER SHIFT"', result.lua)
        self.assertIn('["disable_inhibit"] = true', result.lua)

    def test_invalid_gestures_have_specific_diagnostics(self):
        for content in ("gesture = 3, horizontal", "gesture = 1, left, close", "gesture = 10, up, close",
                        "gesture = 3, sideways, workspace", "gesture = 3, left, unknown: yes, close",
                        "gesture = 3, up, scale: invalid, close", "gesture = 3, up, mod: SUPER",
                        "gesture = 3, left, float, invalid", "gesture = 3, up, close, extra",
                        "gesture = 3, up, fullscreen, maximize, extra", "gesture = 3, up, special, a, b",
                        "gesture = 2, pinch, cursorZoom, nan", "gesture = 2, pinch, cursorZoom, 2, invalid",
                        "gesture = 4, up, dispatcher", "gesture = 4, up, dispatcher, unknown",
                        "gesturez = 3, horizontal, workspace", "gesture = 3, up, unknown"):
            with self.subTest(content=content):
                result = self.convert(content)
                self.assertTrue(result.errors)
                self.assertNotIn("unknown option 'gesture'", result.errors[0].message)

    @unittest.skipUnless(shutil.which("lua"), "Lua interpreter unavailable")
    def test_envd_and_gesture_callback_runtime(self):
        result = self.convert("envd = BACKENDS,wayland,x11\ngesture = 4, up, dispatcher, exec, printf 'a,b'\n")
        self.assertFalse(result.errors)
        harness = '''
local spec, calls = nil, 0
hl = { dsp = { exec_cmd = function(cmd) return cmd end } }
hl.env = function(name, value, dbus)
    assert(name == "BACKENDS" and value == "wayland,x11" and dbus == true)
end
hl.gesture = function(s) spec = s end
hl.dispatch = function(cmd) assert(cmd == "printf 'a,b'"); calls = calls + 1 end
'''
        checks = '''
assert(spec.fingers == 4 and spec.direction == "up")
assert(type(spec.action) == "function" and calls == 0)
spec.action()
assert(calls == 1)
'''
        proc = subprocess.run(["lua", "-"], input=harness + result.lua + checks, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipUnless(shutil.which("lua"), "Lua interpreter unavailable")
    def test_environment_in_shell_commands_and_unset_references(self):
        result = self.convert("bind = SUPER,A,exec,printf '%s' '$KNOWN/$UNKNOWN'\n")
        self.assertFalse(result.errors)
        harness = '''
os.getenv = function(k) if k == "KNOWN" then return "/runtime" end return nil end
hl = { dsp = { exec_cmd = function(cmd) return cmd end } }
hl.bind = function(_, cmd) assert(cmd == "printf '%s' '/runtime/$UNKNOWN'") end
'''
        proc = subprocess.run(["lua", "-"], input=harness + result.lua, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_newline_in_source_filename_stays_commented(self):
        path = self.write("odd\nerror('injected')\n.conf", "general:border_size = 2\n")
        result = Converter().convert(path)
        self.assertFalse(result.errors)
        self.assertIn("\n-- error('injected')", result.lua)

    def test_variables_longest_prefix_and_reassignment(self):
        result = self.convert("$color = ff0000\n$color_long = 00ff00\ngeneral:col.active_border = rgba($color_longff)\n$app = foot\nbind = SUPER,A,exec,$app\n$app = kitty\nbind = SUPER,B,exec,$app")
        self.assertFalse(result.errors)
        self.assertIn('rgba(00ff00ff)', result.lua)
        self.assertIn('exec_cmd("foot")', result.lua)
        self.assertIn('exec_cmd("kitty")', result.lua)

    def test_unsupported_constructs_never_silently_succeed(self):
        for content in ("# hyprlang if MY_VAR\nexec-once = echo hi\n# hyprlang endif",
                        "plugin = /tmp/a.so", "dwindle:pseudotile = true", "bindz = SUPER,A,exec,foot",
                        "general {\nborder_size = 2", "}", "general:border_size = {{1 + 2}}",
                        "bind = SUPER,A,plugin:unknown,arg", "windowrulev2 = mystery,class:foot",
                        "exec-once = echo x && hyprctl dispatch killactive", "source = nonexistent.conf",
                        "device {\nname = mouse\nunknown = 2\n}"):
            with self.subTest(content=content):
                self.assertTrue(self.convert(content).errors)

    def test_typed_values(self):
        self.assertEqual(typed("5 10", "gaps"), dict(top=5, right=10, bottom=5, left=10))
        self.assertEqual(typed("5 10 15", "gaps"), dict(top=5, right=10, bottom=15, left=10))
        self.assertEqual(typed("yes", "bool"), True)
        self.assertEqual(typed("+03", "int"), 3)
        self.assertEqual(typed("rgb(255, 0, 0)", "gradient"), "rgb(255, 0, 0)")
        for value, kind in (("nan", "float"), ("1e999", "float"), ("yes", "int"), ("1.5", "int"), ("maybe", "bool")):
            with self.subTest(value=value):
                self.assertRaises(Unsupported, typed, value, kind)

    @unittest.skipUnless(shutil.which("lua"), "Lua interpreter unavailable")
    def test_lua_escaping_round_trip(self):
        value = 'quotes " \\ control\x00\x01\x7f\t\n\r unicode é λ 🦀'
        code = "io.write(" + lua(value) + ")"
        proc = subprocess.run(["lua", "-"], input=code, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, value.replace("\r", "\n"))

    def test_cli_default_stdout_and_check(self):
        status, stdout, _ = self.cli()
        self.assertEqual(status, 0)
        self.assertIn("hl.bind", stdout)
        status, stdout, _ = self.cli("--check")
        self.assertEqual(status, 0)
        self.assertEqual(stdout, "")

    def test_cli_error_and_partial_status(self):
        self.source.write_text("unknown = 3")
        output = self.root / "hyprland.lua"
        report = self.root / "report.json"
        status, stdout, _ = self.cli("-o", output, "--report", report)
        self.assertEqual(status, 1)
        self.assertFalse(output.exists())
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(report.read_text())["errors"], 1)
        status, stdout, _ = self.cli("--allow-partial")
        self.assertEqual(status, 1)
        self.assertIn("TODO", stdout)

    def test_cli_no_overwrite_backup_and_permissions(self):
        output = self.write("hyprland.lua", "old Lua")
        status, _, _ = self.cli("-o", output)
        self.assertEqual(status, 2)
        self.assertEqual(output.read_text(), "old Lua")
        status, _, _ = self.cli("-o", output, "--force")
        self.assertEqual(status, 0)
        backups = list(self.root.glob("hyprland.lua.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "old Lua")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertIn("hl.bind", output.read_text())
        self.assertEqual(self.source.read_text(), FIXTURE)

    def test_cli_refuses_input_symlink_hardlink_and_report_collisions(self):
        alias = self.root / "alias.lua"
        alias.symlink_to(self.source)
        hardlink = self.root / "hardlink.lua"
        os.link(self.source, hardlink)
        for target in (self.source, alias, hardlink):
            with self.subTest(target=target):
                status, _, _ = self.cli("-o", target, "--force")
                self.assertEqual(status, 2)
                self.assertEqual(self.source.read_text(), FIXTURE)
        status, _, _ = self.cli("-o", self.root / "out.lua", "--report", self.root / "out.lua")
        self.assertEqual(status, 2)
        status, _, _ = self.cli("--report", self.source, "--force")
        self.assertEqual(status, 2)

    def test_cli_dry_run_and_check_never_write(self):
        output = self.root / "out.lua"
        report = self.root / "out.json"
        for flag in ("--dry-run", "--check"):
            status, stdout, _ = self.cli(flag, "-o", output, "--report", report)
            self.assertEqual(status, 0)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())
            self.assertEqual(bool(stdout), flag == "--dry-run")

    def test_safe_write_race_and_failure_cleanup(self):
        path = self.root / "output.lua"
        real_link = os.link

        def raced_link(src, dst):
            path.write_text("raced")
            real_link(src, dst)

        with patch("hyprland_migrator.cli.os.link", side_effect=raced_link):
            self.assertRaises(FileExistsError, safe_write, path, "new")
        self.assertEqual(path.read_text(), "raced")
        self.assertEqual(list(self.root.glob(".output.lua.*")), [])

    def test_missing_entry_exit_status(self):
        self.source.unlink()
        self.assertEqual(self.cli()[0], 2)

    @unittest.skipUnless(shutil.which("Hyprland"), "Hyprland unavailable")
    def test_real_hyprland_verifier(self):
        result = Converter().convert(self.source)
        self.assertFalse(result.errors)
        output = self.write("hyprland.lua", result.lua)
        proc = subprocess.run(["Hyprland", "--verify-config", "-c", str(output)], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("config ok", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
