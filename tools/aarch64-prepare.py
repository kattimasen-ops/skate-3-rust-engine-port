#!/usr/bin/env python3
"""
aarch64-prepare.py — bereitet das Skate-3-Repo für RK3326/M9 Pro vor.

Wendet an:
  1. .cargo/config.toml
  2. Root-Cargo.toml (libc, mimalloc, alle wgpu-* + naga als Pfad-Patches)
  3. skate-game/Cargo.toml (libc, mimalloc, crossbeam-channel, default = [],
                            [lints.rust] dead_code = "allow")
  4. main.rs (Module, WGPU-Env in unsafe-Block, HeadlessFbPlugin, exit-Cast,
              mimalloc global_allocator)
  5. app.rs (Backends::GL, primary_window: None, ExitCondition-Import,
             winit-disable nach .build(), ScheduleRunnerPlugin,
             GpuPreprocessingSupport, kein Box)
  6. fb0_writer.rs (nur wenn fehlt)
  7. headless_plugin.rs (Bevy-0.18-ImageCopy: Async Readback)
  8. portmaster/ (Auto-Generierung von input.cfg)
  9. vendor/wgpu: [package]-Vererbung auflösen
 10. vendor/wgpu: [dependencies]-Vererbung auflösen
 11. vendor/wgpu/wgpu-hal/Cargo.toml: naga mit termcolor-Feature
 12. vendor/wgpu/wgpu-hal/src/gles/adapter.rs (Mali-G31 SSBO-Fix)
 13. wgpu-hal Version verifizieren (27.0.4)
 14. crates/skate-game/src/input/platform.rs: unused XinputCaps-Import
 15. crates/**/*.rs: .fetch_update( → .try_update(
 16. Cargo.lock entfernen (erzwingt Neu-Auflösung der Patches)

Idempotent. Fehlschläge kritischer Patches → Exit 1.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
GAME_SRC    = ROOT / "crates" / "skate-game" / "src"
GAME_CARGO  = ROOT / "crates" / "skate-game" / "Cargo.toml"
ROOT_CARGO  = ROOT / "Cargo.toml"
MAIN_RS     = GAME_SRC / "main.rs"
APP_RS      = GAME_SRC / "app.rs"
FB0_RS      = GAME_SRC / "fb0_writer.rs"
HEADLESS_RS = GAME_SRC / "headless_plugin.rs"
PLATFORM_RS = GAME_SRC / "input" / "platform.rs"
PORTMASTER  = ROOT / "portmaster"
CARGO_CFG   = ROOT / ".cargo" / "config.toml"
WGPU_ROOT   = ROOT / "vendor" / "wgpu"
WGPU_HAL    = WGPU_ROOT / "wgpu-hal"

WGPU_PATCH_ENTRIES = [
    ("wgpu",        "vendor/wgpu/wgpu"),
    ("wgpu-core",   "vendor/wgpu/wgpu-core"),
    ("wgpu-hal",    "vendor/wgpu/wgpu-hal"),
    ("wgpu-types",  "vendor/wgpu/wgpu-types"),
    ("naga",        "vendor/wgpu/naga"),
]

CHECK_ONLY = False
FORCE = False
warnings, errors = [], []

def log(msg): print(f"[prepare] {msg}")
def warn(msg):
    warnings.append(msg)
    print(f"[prepare] WARNUNG: {msg}")
def fail(msg):
    errors.append(msg)
    print(f"[prepare] FEHLER: {msg}", file=sys.stderr)


def ensure_tomlkit():
    try:
        import tomlkit
        return tomlkit
    except ImportError:
        log("Installiere tomlkit via pip...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet", "tomlkit"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            import tomlkit
            return tomlkit
        except Exception as e:
            fail(f"tomlkit konnte nicht installiert werden: {e}")
            return None


def find_matching(text, open_idx, open_char='{', close_char='}'):
    if open_idx >= len(text) or text[open_idx] != open_char:
        return -1
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def write_if_missing(path, content):
    if path.exists() and not FORCE:
        log(f"übersprungen (existiert): {path.relative_to(ROOT)}")
        return False
    if CHECK_ONLY:
        log(f"würde schreiben: {path.relative_to(ROOT)}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    log(f"geschrieben: {path.relative_to(ROOT)}")
    return True


def patch_file(path, patch_fn, description):
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} nicht gefunden ({description})")
        return False
    old = path.read_text()
    new, changed = patch_fn(old)
    if not changed:
        log(f"bereits gepatcht: {description}")
        return True
    if CHECK_ONLY:
        log(f"würde patchen: {description}")
        return True
    path.write_text(new)
    log(f"gepatcht: {description}")
    return True


# ═════════════════════════════════════════════════════════════════════
# 1. .cargo/config.toml
# ═════════════════════════════════════════════════════════════════════
CARGO_CONFIG = """\
[target.aarch64-unknown-linux-gnu]
linker = "aarch64-linux-gnu-gcc"
rustflags = [
    "-C", "target-cpu=cortex-a35",
    "-C", "target-feature=-outline-atomics",
    "-C", "link-arg=-Wl,--gc-sections",
    "-C", "link-arg=-Wl,--as-needed",
    "-C", "link-arg=-Wl,-O2",
    "-C", "link-arg=-Wl,--sort-common",
    "-C", "link-arg=-static-libgcc",
    "-C", "link-arg=-Wl,--allow-shlib-undefined",
    "-C", "panic=abort",
]

[env]
WGPU_BACKEND = "gl"
WGPU_SETTINGS_PRIO = "webgl2"
"""

def prepare_cargo_config():
    log("== .cargo/config.toml ==")
    write_if_missing(CARGO_CFG, CARGO_CONFIG)


# ═════════════════════════════════════════════════════════════════════
# 2. Root-Cargo.toml
# ═════════════════════════════════════════════════════════════════════
def patch_root_cargo(text):
    changed = False

    if 'libc = "0.2"' not in text and "libc.workspace" not in text:
        if 'serde_json = "1.0"' in text:
            text = text.replace('serde_json = "1.0"',
                                'serde_json = "1.0"\nlibc = "0.2"', 1)
            changed = True
        elif "[workspace.dependencies]" in text:
            text = text.replace("[workspace.dependencies]",
                                '[workspace.dependencies]\nlibc = "0.2"', 1)
            changed = True
        else:
            fail("Root-Cargo.toml: kein Anker für libc")
            return text, False

    if 'mimalloc = "0.1"' not in text and "mimalloc.workspace" not in text:
        if 'libc = "0.2"' in text:
            text = text.replace('libc = "0.2"',
                                'libc = "0.2"\nmimalloc = "0.1"', 1)
            changed = True
        elif "[workspace.dependencies]" in text:
            text = text.replace("[workspace.dependencies]",
                                '[workspace.dependencies]\nmimalloc = "0.1"', 1)
            changed = True
        else:
            fail("Root-Cargo.toml: kein Anker für mimalloc")
            return text, False

    if "[patch.crates-io]" not in text:
        warn("Root-Cargo.toml: [patch.crates-io] fehlt")
        return text, changed

    for crate_name, path in WGPU_PATCH_ENTRIES:
        active = f'{crate_name} = {{ path = "{path}" }}'
        commented = f'# {crate_name} = {{ path = "{path}" }}'
        if active in text:
            continue
        if commented in text:
            text = text.replace(commented, active, 1)
            changed = True
            continue
        text = text.replace(
            "[patch.crates-io]",
            f'[patch.crates-io]\n{active}',
            1,
        )
        changed = True

    return text, changed

def prepare_root_cargo():
    log("== Root-Cargo.toml ==")
    if not WGPU_ROOT.exists():
        warn("vendor/wgpu fehlt – wgpu-Patches werden NICHT aktiviert")
    patch_file(ROOT_CARGO, patch_root_cargo, "root Cargo.toml")


# ═════════════════════════════════════════════════════════════════════
# 3. skate-game/Cargo.toml
# ═════════════════════════════════════════════════════════════════════
def patch_game_cargo(text):
    changed = False

    if "libc.workspace" not in text and 'libc = "0.2"' not in text:
        if "[dependencies]" in text:
            text = text.replace("[dependencies]",
                                "[dependencies]\nlibc.workspace = true", 1)
        else:
            text += "\n[dependencies]\nlibc.workspace = true\n"
        changed = True

    if "mimalloc.workspace" not in text and 'mimalloc = "0.1"' not in text:
        if "[dependencies]" in text:
            text = text.replace(
                "[dependencies]",
                "[dependencies]\nmimalloc.workspace = true",
                1,
            )
        else:
            text += "\n[dependencies]\nmimalloc.workspace = true\n"
        changed = True

    if "crossbeam-channel" not in text:
        if "[dependencies]" in text:
            text = text.replace(
                "[dependencies]",
                '[dependencies]\ncrossbeam-channel = "0.5"',
                1,
            )
        else:
            text += '\n[dependencies]\ncrossbeam-channel = "0.5"\n'
        changed = True

    if 'default = ["dev-dynamic"]' in text:
        text = text.replace('default = ["dev-dynamic"]', 'default = []', 1)
        changed = True

    if "dead_code" not in text:
        if "[lints.rust]" not in text:
            text += '\n[lints.rust]\ndead_code = "allow"\n'
        else:
            text = text.replace("[lints.rust]",
                                '[lints.rust]\ndead_code = "allow"', 1)
        changed = True

    return text, changed

def prepare_game_cargo():
    log("== skate-game/Cargo.toml ==")
    if not GAME_CARGO.exists():
        fail(f"{GAME_CARGO.relative_to(ROOT)} nicht gefunden")
        return
    patch_file(GAME_CARGO, patch_game_cargo, "skate-game Cargo.toml")


# ═════════════════════════════════════════════════════════════════════
# 4. main.rs
# ═════════════════════════════════════════════════════════════════════
def patch_main(text):
    changed = False

    if "mod fb0_writer;" not in text:
        if "mod trigger_volumes;" in text:
            text = text.replace("mod trigger_volumes;",
                "mod trigger_volumes;\nmod fb0_writer;\nmod headless_plugin;", 1)
            changed = True
        else:
            warn("main.rs: 'mod trigger_volumes;' fehlt")

    if 'std::env::set_var("WGPU_BACKEND"' not in text:
        if "fn main() -> bevy::app::AppExit {" in text:
            text = text.replace("fn main() -> bevy::app::AppExit {",
                'fn main() -> bevy::app::AppExit {\n'
                '    unsafe {\n'
                '        std::env::set_var("WGPU_BACKEND", "gl");\n'
                '        std::env::set_var("WGPU_SETTINGS_PRIO", "webgl2");\n'
                '    }', 1)
            changed = True
        else:
            warn("main.rs: 'fn main() -> bevy::app::AppExit {' fehlt")
    elif ('std::env::set_var("WGPU_BACKEND"' in text
          and 'unsafe {\n        std::env::set_var' not in text):
        text = text.replace(
            '    std::env::set_var("WGPU_BACKEND", "gl");\n'
            '    std::env::set_var("WGPU_SETTINGS_PRIO", "webgl2");',
            '    unsafe {\n'
            '        std::env::set_var("WGPU_BACKEND", "gl");\n'
            '        std::env::set_var("WGPU_SETTINGS_PRIO", "webgl2");\n'
            '    }',
            1,
        )
        changed = True

    if "headless_plugin::HeadlessFbPlugin" not in text:
        if "let mut app = app::build(config, manifest, graphs, physics, skater);" in text:
            text = text.replace(
                "let mut app = app::build(config, manifest, graphs, physics, skater);",
                "let mut app = app::build(config, manifest, graphs, physics, skater);\n"
                "    app.add_plugins(headless_plugin::HeadlessFbPlugin);", 1)
            changed = True
        else:
            warn("main.rs: app::build(...)-Anker fehlt")

    if "std::process::exit(code)" in text:
        text = text.replace("std::process::exit(code)", "std::process::exit(code as i32)")
        changed = True

    return text, changed

def prepare_main():
    log("== main.rs ==")
    patch_file(MAIN_RS, patch_main, "main.rs")


# ═════════════════════════════════════════════════════════════════════
# 4b. main.rs: mimalloc als Global Allocator
# ═════════════════════════════════════════════════════════════════════
def patch_main_allocator():
    log("== main.rs: mimalloc global_allocator ==")
    if not MAIN_RS.exists():
        fail(f"{MAIN_RS.relative_to(ROOT)} nicht gefunden")
        return
    text = MAIN_RS.read_text()

    if "mimalloc::MiMalloc" in text:
        log("  bereits gepatcht")
        return

    allocator_block = (
        "#[global_allocator]\n"
        "static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;\n\n"
    )

    if "fn main()" in text:
        idx = text.find("fn main()")
        insert_at = text.rfind("\n\n", 0, idx)
        if insert_at < 0:
            insert_at = idx
        else:
            insert_at += 2
        text = text[:insert_at] + allocator_block + text[insert_at:]
    elif "//!" in text:
        last_doc = text.rfind("//!")
        end_of_doc = text.find("\n", last_doc) + 1
        text = text[:end_of_doc] + allocator_block + text[end_of_doc:]
    else:
        text = allocator_block + text

    if CHECK_ONLY:
        log("  würde patchen (global_allocator einfügen)")
        return
    MAIN_RS.write_text(text)
    log("  gepatcht: mimalloc global_allocator eingefügt")


# ═════════════════════════════════════════════════════════════════════
# 5. app.rs
# ═════════════════════════════════════════════════════════════════════
def patch_app(text):
    changed = False

    if "use bevy::window::ExitCondition;" not in text:
        if "use bevy::prelude::*;" in text:
            text = text.replace("use bevy::prelude::*;",
                                "use bevy::prelude::*;\nuse bevy::window::ExitCondition;", 1)
            changed = True
        elif "use bevy::{" in text:
            text = text.replace("use bevy::{",
                                "use bevy::window::ExitCondition;\nuse bevy::{", 1)
            changed = True
        else:
            warn("app.rs: kein Import-Anker für ExitCondition")

    if "Backends::VULKAN" in text:
        text = text.replace("Backends::VULKAN", "Backends::GL", 1)
        changed = True

    if "Automatic(Box::new(WgpuSettings" in text:
        text = text.replace("Automatic(Box::new(WgpuSettings", "Automatic(WgpuSettings", 1)
        idx = text.find("Automatic(WgpuSettings")
        if idx >= 0:
            paren_open = text.find("(", idx + len("Automatic"))
            if paren_open >= 0:
                close = find_matching(text, paren_open, '(', ')')
                if close > 0 and text[close+1:close+2] == ')':
                    text = text[:close+1] + text[close+2:]
        changed = True

    if "primary_window: Some" in text and "primary_window: None" not in text:
        idx = text.find("primary_window: Some")
        j = idx + len("primary_window: Some")
        while j < len(text) and text[j] in ' \t':
            j += 1
        if j < len(text) and text[j] == '(':
            close = find_matching(text, j, '(', ')')
            if close > 0:
                text = text[:idx] + "primary_window: None" + text[close + 1:]
                changed = True

    if "ExitCondition::DontExit" not in text:
        anchor = "primary_window: None"
        idx = text.find(anchor)
        if idx >= 0:
            insert_at = idx + len(anchor)
            j = insert_at
            while j < len(text) and text[j] in ' \t\n':
                j += 1
            if j < len(text) and text[j] == ',':
                text = (text[:j + 1]
                        + "\n            exit_condition: ExitCondition::DontExit,"
                        + text[j + 1:])
            else:
                text = (text[:insert_at]
                        + ",\n            exit_condition: ExitCondition::DontExit"
                        + text[insert_at:])
            changed = True
        else:
            warn("app.rs: 'primary_window: None' fehlt – exit_condition nicht eingefügt")

    if ("disable::<WinitPlugin>" not in text
            and "disable::<bevy::winit::WinitPlugin>" not in text):
        if ".build()" in text:
            pos = text.find(".build()")
            insert_at = pos + len(".build()")
            text = (text[:insert_at]
                    + ".disable::<bevy::winit::WinitPlugin>()"
                    + text[insert_at:])
            changed = True
        else:
            warn("app.rs: '.build()' fehlt – winit-disable nicht eingefügt")

    if "ScheduleRunnerPlugin" not in text:
        m = re.search(r'app\.add_plugins\s*\(\s*\n?\s*DefaultPlugins', text)
        if m:
            open_paren = text.find("(", m.start())
            if open_paren >= 0:
                close_paren = find_matching(text, open_paren, '(', ')')
                if close_paren > 0:
                    insert = (
                        "\n    .add_plugins(bevy::app::ScheduleRunnerPlugin::run_loop(\n"
                        "        std::time::Duration::from_secs_f64(1.0 / 30.0),\n"
                        "    ))"
                    )
                    text = text[:close_paren + 1] + insert + text[close_paren + 1:]
                    changed = True
                else:
                    warn("app.rs: DefaultPlugins-Klammer nicht geschlossen")
            else:
                warn("app.rs: DefaultPlugins-Öffnungsklammer nicht gefunden")
        else:
            warn("app.rs: 'app.add_plugins(DefaultPlugins' nicht gefunden")

    if "GpuPreprocessingSupport" not in text:
        idx = text.rfind("\n    app\n")
        if idx >= 0:
            insert = (
                "\n    if let Some(render_app) = app.get_sub_app_mut(bevy::render::RenderApp) {\n"
                "        render_app.insert_resource(bevy::render::batching::gpu_preprocessing::GpuPreprocessingSupport {\n"
                "            max_supported_mode: bevy::render::batching::gpu_preprocessing::GpuPreprocessingMode::None,\n"
                "        });\n"
                "    }\n"
            )
            text = text[:idx] + insert + "\n    app" + text[idx + len("\n    app"):]
            changed = True
        else:
            warn("app.rs: '\\n    app\\n' als Anker für GpuPreprocessingSupport fehlt")

    return text, changed

def prepare_app():
    log("== app.rs ==")
    if not APP_RS.exists():
        fail(f"{APP_RS.relative_to(ROOT)} nicht gefunden – app.rs-Patch kritisch")
        return
    patch_file(APP_RS, patch_app, "app.rs")


# ═════════════════════════════════════════════════════════════════════
# 6. fb0_writer.rs (nur wenn fehlt)
# ═════════════════════════════════════════════════════════════════════
FB0_RS_FALLBACK = r'''//! /dev/fb0-Writer für RK3326 / M9 Pro.
use std::fs::{File, OpenOptions};
use std::io;
use std::os::unix::io::AsRawFd;
use std::ptr;

#[repr(C)]
#[derive(Default)]
struct FbBitfield { offset: u32, length: u32, msb_right: u32 }

#[repr(C)]
#[derive(Default)]
struct FbVarScreeninfo {
    xres: u32, yres: u32, xres_virtual: u32, yres_virtual: u32,
    xoffset: u32, yoffset: u32, bits_per_pixel: u32, grayscale: u32,
    red: FbBitfield, green: FbBitfield, blue: FbBitfield, transp: FbBitfield,
    nonstd: u32, activate: u32, height: u32, width: u32, accel_flags: u32,
    pixclock: u32, left_margin: u32, right_margin: u32, upper_margin: u32,
    lower_margin: u32, hsync_len: u32, vsync_len: u32, sync: u32, vmode: u32,
    rotate: u32, colorspace: u32, reserved: [u32; 4],
}

#[repr(C)]
#[derive(Default)]
struct FbFixScreeninfo {
    id: [u8; 16], smem_start: usize, smem_len: u32, type_: u32, type_aux: u32,
    visual: u32, xpanstep: u16, ypanstep: u16, ywrapstep: u16, line_length: u32,
    mmio_start: usize, mmio_len: u32, accel: u32, capabilities: u16, reserved: [u16; 2],
}

const FBIOGET_VSCREENINFO: u64 = 0x4600;
const FBIOGET_FSCREENINFO: u64 = 0x4602;

pub struct Fb0Writer {
    file: File,
    var: FbVarScreeninfo,
    fix: FbFixScreeninfo,
    mmap_ptr: *mut u8,
    mmap_len: usize,
}

unsafe impl Send for Fb0Writer {}
unsafe impl Sync for Fb0Writer {}

impl Fb0Writer {
    pub fn open() -> io::Result<Self> {
        let file = OpenOptions::new().read(true).write(true).open("/dev/fb0")?;
        let fd = file.as_raw_fd();
        let mut var = FbVarScreeninfo::default();
        if unsafe { libc::ioctl(fd, FBIOGET_VSCREENINFO, &mut var) } < 0 {
            return Err(io::Error::last_os_error());
        }
        let mut fix = FbFixScreeninfo::default();
        if unsafe { libc::ioctl(fd, FBIOGET_FSCREENINFO, &mut fix) } < 0 {
            return Err(io::Error::last_os_error());
        }
        let mmap_len = fix.smem_len as usize;
        let mmap_ptr = unsafe {
            libc::mmap(ptr::null_mut(), mmap_len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED, fd, 0)
        };
        if mmap_ptr == libc::MAP_FAILED { return Err(io::Error::last_os_error()); }
        Ok(Self { file, var, fix, mmap_ptr: mmap_ptr as *mut u8, mmap_len })
    }
    pub fn width(&self) -> u32 { self.var.xres }
    pub fn height(&self) -> u32 { self.var.yres }
    pub fn bits_per_pixel(&self) -> u32 { self.var.bits_per_pixel }

    pub fn clear(&mut self) -> io::Result<()> {
        let black = vec![0u8; (self.var.xres * self.var.yres * 4) as usize];
        self.write_rgba8(&black, self.var.xres, self.var.yres)
    }

    pub fn write_rgba8(&mut self, rgba: &[u8], width: u32, height: u32) -> io::Result<()> {
        if rgba.len() < (width * height * 4) as usize {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "RGBA-Puffer zu klein"));
        }
        let line_len = self.fix.line_length as usize;
        match self.var.bits_per_pixel {
            32 => for y in 0..height as usize {
                let src = &rgba[y * width as usize * 4..];
                let dst = unsafe { self.mmap_ptr.add(y * line_len) as *mut u32 };
                for x in 0..width as usize {
                    let r = src[x * 4] as u32;
                    let g = src[x * 4 + 1] as u32;
                    let b = src[x * 4 + 2] as u32;
                    unsafe { *dst.add(x) = (r << 16) | (g << 8) | b; }
                }
            },
            16 => for y in 0..height as usize {
                let src = &rgba[y * width as usize * 4..];
                let dst = unsafe { self.mmap_ptr.add(y * line_len) as *mut u16 };
                for x in 0..width as usize {
                    let r = (src[x * 4] >> 3) as u16;
                    let g = (src[x * 4 + 1] >> 2) as u16;
                    let b = (src[x * 4 + 2] >> 3) as u16;
                    unsafe { *dst.add(x) = (r << 11) | (g << 5) | b; }
                }
            },
            other => return Err(io::Error::new(io::ErrorKind::Unsupported,
                format!("Framebuffer mit {other} bpp wird nicht unterstützt"))),
        }
        let _ = &self.file;
        Ok(())
    }
}

impl Drop for Fb0Writer {
    fn drop(&mut self) { unsafe { libc::munmap(self.mmap_ptr as *mut _, self.mmap_len); } }
}
'''

def prepare_fb0():
    log("== fb0_writer.rs ==")
    if FB0_RS.exists():
        log("  existiert bereits (deine Version wird verwendet)")
        return
    write_if_missing(FB0_RS, FB0_RS_FALLBACK)


# ═════════════════════════════════════════════════════════════════════
# 7. headless_plugin.rs (ImageCopy-API)
# ═════════════════════════════════════════════════════════════════════
HEADLESS_RS_FALLBACK = r'''//! Headless-Rendering -> /dev/fb0 via ImageCopyPlugin (asynchroner Readback).
//!
//! R36S-Bevy0.18-ImageCopy: Nutzt ImageCopyPlugin + persistenten Buffer
//! + crossbeam-Channel statt ScreenshotPlugin (synchroner Readback = Stall).
use bevy::prelude::*;
use bevy::camera::{ImageRenderTarget, RenderTarget};
use bevy::camera::visibility::RenderLayers;
use bevy::render::view::NoIndirectDrawing;
use bevy::render::render_resource::{
    Buffer, BufferDescriptor, BufferUsages, CommandEncoderDescriptor,
    Extent3d, MapMode, PollType, TexelCopyBufferInfo, TexelCopyBufferLayout,
    TextureDescriptor, TextureDimension, TextureFormat, TextureUsages,
};
use bevy::render::renderer::RenderDevice;
use bevy::render::render_asset::RenderAssets;
use bevy::render::render_graph::{self, RenderGraph, RenderLabel};
use bevy::render::texture::GpuImage;
use bevy::render::{Extract, Render, RenderApp, RenderSystems};
use bevy::ui::IsDefaultUiCamera;

use crossbeam_channel::{Receiver, Sender};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

use crate::fb0_writer::Fb0Writer;

const RENDER_WIDTH: u32 = 448;
const RENDER_HEIGHT: u32 = 336;

pub struct HeadlessFbPlugin;

impl Plugin for HeadlessFbPlugin {
    fn build(&self, app: &mut App) {
        let (tx, rx) = crossbeam_channel::unbounded();

        let render_app = app.sub_app_mut(RenderApp);
        render_app
            .insert_resource(RenderWorldSender(tx))
            .add_systems(ExtractSchedule, image_copy_extract)
            .add_systems(
                Render,
                receive_image_from_buffer.after(RenderSystems::Render),
            );

        let mut render_graph = render_app.world_mut().resource_mut::<RenderGraph>();
        render_graph.add_node(ImageCopy, ImageCopyDriver);
        render_graph.add_node_edge(
            ImageCopy,
            bevy::render::graph::CameraDriverLabel,
        );

        app.insert_resource(MainWorldReceiver(rx))
            .init_resource::<FbState>()
            .add_systems(Startup, (clear_fb0, setup_offscreen).chain())
            .add_systems(PostUpdate, write_to_fb0);
    }
}

#[derive(Resource)]
struct MainWorldReceiver(Receiver<Vec<u8>>);

#[derive(Resource)]
struct RenderWorldSender(Sender<Vec<u8>>);

#[derive(Resource)]
struct OffscreenTarget {
    handle: Handle<Image>,
}

#[derive(Resource, Default)]
struct FbState {
    writer: Option<Fb0Writer>,
}

fn clear_fb0(mut state: ResMut<FbState>) {
    if let Ok(mut w) = Fb0Writer::open() {
        let _ = w.clear();
    }
    state.writer = Fb0Writer::open().ok();
}

fn setup_offscreen(
    mut commands: Commands,
    mut images: ResMut<Assets<Image>>,
    render_device: Res<RenderDevice>,
    state: Res<FbState>,
) {
    let size = Extent3d {
        width: RENDER_WIDTH,
        height: RENDER_HEIGHT,
        depth_or_array_layers: 1,
    };

    let mut image = Image {
        texture_descriptor: TextureDescriptor {
            label: Some("skate3_offscreen"),
            size,
            dimension: TextureDimension::D2,
            format: TextureFormat::Rgba8UnormSrgb,
            mip_level_count: 1,
            sample_count: 1,
            usage: TextureUsages::TEXTURE_BINDING
                | TextureUsages::COPY_DST
                | TextureUsages::COPY_SRC
                | TextureUsages::RENDER_ATTACHMENT,
            view_formats: &[],
        },
        ..default()
    };
    image.resize(size);
    let handle = images.add(image);

    commands.spawn((
        Camera3d::default(),
        RenderTarget::Image(ImageRenderTarget {
            handle: handle.clone(),
            scale_factor: 1.0,
        }),
        NoIndirectDrawing,
        RenderLayers::default(),
        IsDefaultUiCamera,
    ));

    commands.insert_resource(OffscreenTarget {
        handle: handle.clone(),
    });

    commands.spawn(ImageCopier::new(handle, size, &render_device));

    if let Some(w) = &state.writer {
        info!(
            "fb0: {}x{}, {} bpp (ImageCopyPlugin, async readback)",
            w.width(),
            w.height(),
            w.bits_per_pixel()
        );
    } else {
        warn!("fb0 konnte nicht geoeffnet werden");
    }
}

fn write_to_fb0(mut state: ResMut<FbState>, receiver: Res<MainWorldReceiver>) {
    let Some(writer) = state.writer.as_mut() else {
        return;
    };
    while let Ok(data) = receiver.0.try_recv() {
        let _ = writer.write_rgba8(&data, RENDER_WIDTH, RENDER_HEIGHT);
    }
}

#[derive(Clone, Default, Resource, Deref, DerefMut)]
struct ImageCopiers(Vec<ImageCopier>);

#[derive(Clone, Component)]
struct ImageCopier {
    buffer: Buffer,
    enabled: Arc<AtomicBool>,
    src_image: Handle<Image>,
}

impl ImageCopier {
    fn new(src_image: Handle<Image>, size: Extent3d, render_device: &RenderDevice) -> Self {
        let padded_bytes_per_row =
            RenderDevice::align_copy_bytes_per_row(size.width as usize * 4);
        let cpu_buffer = render_device.create_buffer(&BufferDescriptor {
            label: Some("skate3_readback_buffer"),
            size: padded_bytes_per_row as u64 * size.height as u64,
            usage: BufferUsages::MAP_READ | BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        Self {
            buffer: cpu_buffer,
            enabled: Arc::new(AtomicBool::new(true)),
            src_image,
        }
    }

    fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Relaxed)
    }
}

fn image_copy_extract(mut commands: Commands, image_copiers: Extract<Query<&ImageCopier>>) {
    commands.insert_resource(ImageCopiers(
        image_copiers.iter().cloned().collect::<Vec<_>>(),
    ));
}

#[derive(Debug, PartialEq, Eq, Clone, Hash, RenderLabel)]
struct ImageCopy;

#[derive(Default)]
struct ImageCopyDriver;

impl render_graph::Node for ImageCopyDriver {
    fn run(
        &self,
        _graph: &mut render_graph::RenderGraphContext,
        render_context: &mut bevy::render::renderer::RenderContext,
        world: &World,
    ) -> Result<(), render_graph::NodeRunError> {
        let Some(image_copiers) = world.get_resource::<ImageCopiers>() else {
            return Ok(());
        };
        let Some(gpu_images) = world.get_resource::<RenderAssets<GpuImage>>() else {
            return Ok(());
        };

        for image_copier in image_copiers.iter() {
            if !image_copier.enabled() {
                continue;
            }
            let Some(src_image) = gpu_images.get(&image_copier.src_image) else {
                continue;
            };

            let mut encoder = render_context
                .render_device()
                .create_command_encoder(&CommandEncoderDescriptor {
                    label: Some("skate3_image_copy_encoder"),
                });

            let block_dimensions = src_image.texture_format.block_dimensions();
            let block_size = src_image
                .texture_format
                .block_copy_size(None)
                .expect("texture format has no block copy size");

            let padded_bytes_per_row = RenderDevice::align_copy_bytes_per_row(
                (src_image.texture_descriptor.size.width as usize
                    / block_dimensions.0 as usize)
                    * block_size as usize,
            );

            encoder.copy_texture_to_buffer(
                src_image.texture.as_image_copy(),
                TexelCopyBufferInfo {
                    buffer: &image_copier.buffer,
                    layout: TexelCopyBufferLayout {
                        offset: 0,
                        bytes_per_row: std::num::NonZero::<u32>::new(
                            padded_bytes_per_row as u32,
                        )
                        .map(Into::into),
                        rows_per_image: None,
                    },
                },
                src_image.texture_descriptor.size,
            );

            render_context.add_command_buffer(encoder.finish());
        }
        Ok(())
    }
}

fn receive_image_from_buffer(
    image_copiers: Option<Res<ImageCopiers>>,
    render_device: Res<RenderDevice>,
    sender: Res<RenderWorldSender>,
) {
    let Some(image_copiers) = image_copiers else {
        return;
    };
    for image_copier in image_copiers.0.iter() {
        if !image_copier.enabled() {
            continue;
        }
        let buffer_slice = image_copier.buffer.slice(..);
        let (tx, rx) = crossbeam_channel::bounded(1);

        buffer_slice.map_async(MapMode::Read, move |result| {
            let _ = tx.send(result);
        });

        let poll_result = render_device.poll(PollType::Wait);
        if poll_result.is_err() {
            let _ = rx.try_recv();
            continue;
        }

        let Ok(Ok(())) = rx.recv() else {
            continue;
        };

        let mapped = buffer_slice.get_mapped_range();
        let _ = sender.send(mapped.to_vec());
        drop(mapped);
        image_copier.buffer.unmap();
    }
}
'''

OLD_HEADLESS_MARKERS = (
    "MessageReader<ScreenshotCaptured>",
    "Trigger<ScreenshotCaptured>",
    "R36S-Bevy0.18-Fix",
    "R36S-Bevy0.18-Option-Data",
)


def prepare_headless():
    log("== headless_plugin.rs ==")
    if HEADLESS_RS.exists():
        old = HEADLESS_RS.read_text()

        if "R36S-Bevy0.18-ImageCopy" in old:
            log("  bereits auf ImageCopy-API gepatcht")
            return

        if any(m in old for m in OLD_HEADLESS_MARKERS):
            log("  alte Screenshot-Version erkannt – ersetze durch ImageCopy-Version")
            if not CHECK_ONLY:
                HEADLESS_RS.write_text(HEADLESS_RS_FALLBACK)
                log(f"  gepatcht: {HEADLESS_RS.relative_to(ROOT)} (R36S-Bevy0.18-ImageCopy)")
            else:
                log("  würde patchen (CHECK_ONLY)")
            return

        log("  existiert bereits (deine Version wird verwendet)")
        return

    write_if_missing(HEADLESS_RS, HEADLESS_RS_FALLBACK)


# ═════════════════════════════════════════════════════════════════════
# 8. portmaster/
# ═════════════════════════════════════════════════════════════════════
PORT_JSON = '''\
{
  "version": 4,
  "name": "skate3.zip",
  "items": ["Skate3.sh", "skate3"],
  "items_opt": [],
  "attr": {
    "title": "Skate 3",
    "porter": ["kattimasen-ops"],
    "desc": "Skate 3 native ARM64 port for RK3326 (M9 Pro / ArkOS).",
    "desc_md": null,
    "inst": "Copy your .pak files into the skate3/assets/ folder.",
    "inst_md": null,
    "genres": ["sports"],
    "image": null,
    "rtr": false,
    "exp": true,
    "runtime": [],
    "store": [],
    "availability": "full",
    "reqs": [],
    "arch": ["aarch64"],
    "min_glibc": "2.30"
  }
}
'''

README_MD = '''\
## Skate 3 - RK3326 Port

Native ARM64 port of **Skate 3** for the **M9 Pro / RK3326 / ArkOS4Clone**.

### Game data
Place your `.pak` files in `skate3/assets/`. The port starts as soon as
`skate3/assets/*.pak` is present.

### Performance (RK3326)
- 448x336, shadows off, NPC draw dist 10u, traffic/ped 25%, V-Sync off
'''

GAMEINFO_XML = '''\
<?xml version="1.0"?>
<gameList>
  <game>
    <path>./Skate3.sh</path>
    <name>Skate 3</name>
    <desc>Native ARM64 port of Skate 3 for RK3326 handhelds.</desc>
    <image>./screenshot.jpg</image>
    <thumbnail>./screenshot.jpg</thumbnail>
    <rating>0.8</rating>
    <releasedate>20100511T000000</releasedate>
    <developer>EA Black Box</developer>
    <publisher>Electronic Arts</publisher>
    <genre>Sports</genre>
    <players>1</players>
  </game>
</gameList>
'''

LAUNCHER = r'''#!/bin/bash
# Skate3.sh — Diagnose-Version mit vollem Logging und Timeouts.
#
# Ziel: Auf einem Gerät ohne SSH-Terminal diagnostizierbar sein.
# - Jede Phase hat einen nummerierten Marker.
# - Jeder potenziell hängende Befehl läuft mit `timeout -s KILL`.
# - Ein Heartbeat-Subprozess schreibt alle 5 s "alive" in die Log-Datei.
# - SKATE3_ASSETS wird gesetzt, damit die Engine die .pak-Dateien findet.
# - Erstellt automatisch input.cfg, falls sie fehlt (verhindert Crash).

set -uo pipefail  # KEIN -e — wir wollen weitermachen auch bei Fehlern

TS() { date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "?"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
GAMEDIR="$SCRIPT_DIR/skate3"

LOG="$GAMEDIR/log.txt"
if ! { [ -d "$GAMEDIR" ] && [ -w "$GAMEDIR" ]; }; then
    LOG="/tmp/skate3-log.txt"
fi

if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    mv -f "$LOG" "$LOG.1" 2>/dev/null || true
fi

: > "$LOG"
exec >> "$LOG" 2>&1

printf '[%s] === Skate3 START (log=%s) ===\n' "$(TS)" "$LOG"
sync 2>/dev/null || true

log_count=0
log() {
    printf '[%s] %s\n' "$(TS)" "$*"
    log_count=$((log_count + 1))
    if [ "$log_count" -ge 10 ]; then
        sync 2>/dev/null || true
        log_count=0
    fi
}
log_sync() {
    printf '[%s] %s\n' "$(TS)" "$*"
    sync 2>/dev/null || true
}

(
    while true; do
        sleep 5
        printf '[%s] [heartbeat] alive\n' "$(TS)" >> "$LOG"
        sync 2>/dev/null || true
    done
) &
HEARTBEAT_PID=$!

log "SCRIPT_DIR=$SCRIPT_DIR"
log "GAMEDIR=$GAMEDIR"
log "LOG=$LOG"
log "Kernel=$(uname -r 2>/dev/null || echo '?'), Arch=$(uname -m 2>/dev/null || echo '?')"
log "PID=$$"
log "MemAvailable: $(grep MemAvailable /proc/meminfo 2>/dev/null | awk '{print $2 "kB"}' || echo '?')"

if [ ! -x "$GAMEDIR/skate3" ]; then
    log_sync "FEHLER: $GAMEDIR/skate3 fehlt oder nicht ausführbar"
    kill "$HEARTBEAT_PID" 2>/dev/null || true
    exit 1
fi
log_sync "[ok] Binary $GAMEDIR/skate3 ist ausführbar"

run_step() {
    local tmo="$1"; shift
    local label="$*"
    log_sync "[start] $label (timeout ${tmo}s)"
    local rc=0
    if command -v timeout >/dev/null 2>&1; then
        timeout -s KILL "$tmo" "$@" 2>&1
        rc=$?
    else
        "$@" 2>&1
        rc=$?
    fi
    if [ "$rc" -eq 124 ]; then
        log_sync "[TIMEOUT] $label (${tmo}s) — übersprungen"
    elif [ "$rc" -eq 0 ]; then
        log "[ok] $label (rc=0)"
    else
        log "[fail] $label (rc=$rc)"
    fi
    return 0
}

run_step_q() {
    local tmo="$1"; shift
    local label="$*"
    log_sync "[start] $label (timeout ${tmo}s)"
    local rc=0
    if command -v timeout >/dev/null 2>&1; then
        timeout -s KILL "$tmo" "$@" >/dev/null 2>&1
        rc=$?
    else
        "$@" >/dev/null 2>&1
        rc=$?
    fi
    if [ "$rc" -eq 124 ]; then
        log_sync "[TIMEOUT] $label (${tmo}s) — übersprungen"
    elif [ "$rc" -eq 0 ]; then
        log "[ok] $label"
    else
        log "[fail] $label (rc=$rc)"
    fi
    return 0
}

log_sync ""
log_sync "== [1/12] PortMaster control.txt =="
controlfolder=""
for candidate in "/opt/system/Tools/PortMaster" "/opt/tools/PortMaster" \
                 "$HOME/.local/share/PortMaster" "/roms/ports/PortMaster" \
                 "/roms/tools/PortMaster"; do
    if [ -d "$candidate" ]; then controlfolder="$candidate"; break; fi
done
log "controlfolder=$controlfolder"

if [ -n "$controlfolder" ] && [ -f "$controlfolder/control.txt" ]; then
    PM_ENV="/tmp/pm-env.$$"
    log_sync "[start] source $controlfolder/control.txt (subshell, timeout 5s)"
    timeout -s KILL 5 bash -c "set -a; source '$controlfolder/control.txt' 2>/dev/null; set +a; env" > "$PM_ENV" 2>&1
    SRC_RC=$?
    if [ "$SRC_RC" -eq 124 ]; then
        log_sync "[TIMEOUT] control.txt — fahre ohne fort"
    elif [ -s "$PM_ENV" ]; then
        for var in SDL_GAMECONTROLLERCONFIG sdl_controllerconfig CFW_NAME; do
            val="$(grep "^$var=" "$PM_ENV" 2>/dev/null | head -1 | cut -d= -f2- || true)"
            if [ -n "$val" ]; then
                export "$var=$val"
                log "  importiert: $var=$val"
            fi
        done
        log "[ok] control.txt (env ausgelesen)"
    else
        log "[skip] control.txt (leer oder Fehler)"
    fi
    rm -f "$PM_ENV"
else
    log "[skip] control.txt nicht gefunden"
fi

log_sync ""
log_sync "== [2/12] SDL-Umgebung =="
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-kmsdrm}"
export SDL_AUDIODRIVER=alsa
unset AUDIODEV
export SDL_AUDIO_SAMPLES="${SDL_AUDIO_SAMPLES:-2048}"

if [ -n "${SDL_GAMECONTROLLERCONFIG:-}" ]; then
    export SDL_GAMECONTROLLERCONFIG_FILE="/tmp/skate3-gamecontrollerdb.txt"
    printf '%s\n' "$SDL_GAMECONTROLLERCONFIG" > "$SDL_GAMECONTROLLERCONFIG_FILE" 2>/dev/null || true
    log "SDL_GAMECONTROLLERCONFIG_FILE gesetzt"
fi

log_sync ""
log_sync "== [3/12] ALSA-Mixer =="
if command -v amixer >/dev/null 2>&1; then
    for ctrl in Playback Master PCM Speaker Headphone DAC; do
        run_step_q 3 amixer -c 0 sset "$ctrl" 100% unmute
    done
    run_step_q 3 amixer -c 0 cset numid=1 1
fi

log_sync ""
log_sync "== [4/12] .asoundrc =="
ASOUNDRC="$HOME/.asoundrc"
if [ -f "$ASOUNDRC" ]; then
    if ! grep -q 'type hw' "$ASOUNDRC" 2>/dev/null || \
       ! grep -q 'card 0' "$ASOUNDRC" 2>/dev/null; then
        mv -f "$ASOUNDRC" "$ASOUNDRC.skate3-backup" 2>/dev/null || rm -f "$ASOUNDRC"
        log "Alte .asoundrc gesichert (nicht hw:0)"
    fi
fi

log_sync ""
log_sync "== [5/12] CPU-Governor =="
CPU_GOV_PATHS=()
CPU_GOV_SAVED=()
for c in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor \
         /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -f "$c" ] || continue
    [ -w "$c" ] || continue
    old="$(cat "$c" 2>/dev/null || echo '')"
    CPU_GOV_PATHS+=("$c")
    CPU_GOV_SAVED+=("$old")
    timeout -s KILL 2 sh -c "echo performance > '$c'" 2>/dev/null || true
done
log "[done] CPU-Governor (${#CPU_GOV_PATHS[@]} Pfade)"

log_sync ""
log_sync "== [6/12] GPU-Governor =="
gpu_devfreq=""
for candidate in /sys/class/devfreq/*.gpu /sys/class/devfreq/gpu; do
    [ -d "$candidate" ] && gpu_devfreq="$candidate" && break
done
gpu_gov_saved=""
gpu_min_saved=""
if [ -n "$gpu_devfreq" ]; then
    [ -r "$gpu_devfreq/governor" ] && gpu_gov_saved=$(cat "$gpu_devfreq/governor" 2>/dev/null || echo "")
    [ -w "$gpu_devfreq/governor" ] && timeout -s KILL 2 sh -c "echo performance > '$gpu_devfreq/governor'" 2>/dev/null || true
    if [ -r "$gpu_devfreq/available_frequencies" ] && [ -w "$gpu_devfreq/min_freq" ]; then
        gpu_min_saved=$(cat "$gpu_devfreq/min_freq" 2>/dev/null || echo "")
        max_freq=$(tr ' ' '\n' < "$gpu_devfreq/available_frequencies" 2>/dev/null | sort -n | tail -n 1 || echo "")
        [ -n "$max_freq" ] && timeout -s KILL 2 sh -c "echo $max_freq > '$gpu_devfreq/min_freq'" 2>/dev/null || true
    fi
fi

log_sync ""
log_sync "== [7/12] ZRAM =="
if swapon --show 2>/dev/null | grep -q zram; then
    log "[skip] ZRAM bereits aktiv"
else
    run_step_q 5 modprobe zram
    if [ -e /dev/zram0 ]; then
        grep -q lz4 /sys/block/zram0/comp_algorithm 2>/dev/null && \
            run_step_q 2 sh -c 'echo lz4 > /sys/block/zram0/comp_algorithm'
        run_step_q 2 sh -c 'echo 512M > /sys/block/zram0/disksize'
        run_step_q 3 mkswap /dev/zram0
        run_step_q 3 swapon /dev/zram0
    fi
fi

log_sync ""
log_sync "== [8/12] DRI-Rechte =="
for node in /dev/dri/card0 /dev/dri/renderD128 /dev/fb0; do
    if [ -e "$node" ]; then
        if [ ! -r "$node" ] || [ ! -w "$node" ]; then
            run_step_q 2 sudo -n chmod 666 "$node"
            run_step_q 2 chmod 666 "$node"
        fi
    fi
done

log_sync ""
log_sync "== [9/12] Mali-Libs =="
SYSTEM_MALI=""
for candidate in \
    /usr/lib/aarch64-linux-gnu/libmali-bifrost-g31-rxp0-gbm.so \
    /usr/local/lib/aarch64-linux-gnu/libmali-bifrost-g31-rxp0-gbm.so \
    /usr/lib/aarch64-linux-gnu/libmali.so.1 \
    /usr/lib/aarch64-linux-gnu/libmali.so \
    /usr/lib/aarch64-linux-gnu/libEGL.so.1; do
    if [ -f "$candidate" ]; then SYSTEM_MALI="$candidate"; break; fi
done
if [ -n "$SYSTEM_MALI" ]; then
    SKIP_MALI_COPY=0
    if [ -d /tmp/skate3-mali ] && \
       [ -f /tmp/skate3-mali/libmali.so ] && \
       [ "$(stat -c%s /tmp/skate3-mali/libmali.so 2>/dev/null)" = "$(stat -c%s "$SYSTEM_MALI" 2>/dev/null)" ]; then
        SKIP_MALI_COPY=1
    fi
    if [ "$SKIP_MALI_COPY" = "0" ]; then
        rm -rf /tmp/skate3-mali
        mkdir -p /tmp/skate3-mali
        for name in libmali.so.0 libmali.so.1 libmali.so \
                    libEGL.so.1 libEGL.so \
                    libGLESv2.so.2 libGLESv2.so; do
            cp -f "$SYSTEM_MALI" "/tmp/skate3-mali/$name" 2>/dev/null || true
        done
        log "Mali-Blob kopiert."
    else
        log "Mali-Blob bereits gecacht in /tmp."
    fi
    export LD_LIBRARY_PATH="/tmp/skate3-mali:$GAMEDIR:/usr/lib/aarch64-linux-gnu:${LD_LIBRARY_PATH:-}"
else
    export LD_LIBRARY_PATH="$GAMEDIR:/usr/lib/aarch64-linux-gnu:${LD_LIBRARY_PATH:-}"
    log "WARN: kein Mali-Blob gefunden"
fi

log_sync ""
log_sync "== [10/12] Restore-Handler =="
restored=0
restore() {
    [ "$restored" = "1" ] && return 0
    restored=1
    for i in "${!CPU_GOV_PATHS[@]}"; do
        old="${CPU_GOV_SAVED[$i]}"
        if [ -n "$old" ]; then
            timeout -s KILL 2 sh -c "echo '$old' > '${CPU_GOV_PATHS[$i]}'" 2>/dev/null || true
        fi
    done
    if [ -n "$gpu_devfreq" ]; then
        [ -n "$gpu_gov_saved" ] && timeout -s KILL 2 sh -c "echo '$gpu_gov_saved' > '$gpu_devfreq/governor'" 2>/dev/null || true
        [ -n "$gpu_min_saved" ] && timeout -s KILL 2 sh -c "echo '$gpu_min_saved' > '$gpu_devfreq/min_freq'" 2>/dev/null || true
    fi
}
cleanup_all() {
    kill "$HEARTBEAT_PID" 2>/dev/null || true
    if [ -n "${GPTOKEYB_PID:-}" ] && kill -0 "$GPTOKEYB_PID" 2>/dev/null; then
        kill -TERM "$GPTOKEYB_PID" 2>/dev/null || true
        sleep 0.3
        kill -KILL "$GPTOKEYB_PID" 2>/dev/null || true
    fi
    restore
    sync
}
trap cleanup_all EXIT INT TERM HUP

export SKATE3_ASSETS="$GAMEDIR/assets"
log "SKATE3_ASSETS=$SKATE3_ASSETS"

CONFIG_DIR="$GAMEDIR/assets/private/stock/data/config"
CONFIG_FILE="$CONFIG_DIR/input.cfg"

if [ ! -f "$CONFIG_FILE" ]; then
    log "Erstelle fehlende input.cfg in $CONFIG_DIR"
    mkdir -p "$CONFIG_DIR"
    cat > "$CONFIG_FILE" << 'EOF'
// Skate 3 Stock Input Configuration
DPadU = Button0;
DPadD = Button1;
DPadL = Button2;
DPadR = Button3;
Start = Button4;
Back = Button5;
LStick = Button6;
RStick = Button7;
LBumper = Button8;
RBumper = Button9;
LTrigger = Button10;
RTrigger = Button11;
A = Button12;
B = Button13;
X = Button14;
Y = Button15;
LStickR = Button16;
LStickL = Button17;
LStickU = Button18;
LStickD = Button19;
RStickR = Button20;
RStickL = Button21;
RStickU = Button22;
RStickD = Button23;

GP_LStickX = LStickR-LStickL;
GP_LStickY = LStickU-LStickD;
GP_LStickIn = LStick;
GP_RStickX = RStickR-RStickL;
GP_RStickY = RStickU-RStickD;
GP_RStickIn = RStick;
GP_LTrigger = LTrigger;
GP_RTrigger = RTrigger;
GP_LBumper = LBumper;
GP_RBumper = RBumper;
GP_UDPad = DPadU;
GP_DDPad = DPadD;
GP_LDPad = DPadL;
GP_RDPad = DPadR;
GP_XFace = X;
GP_YFace = Y;
GP_AFace = A;
GP_BFace = B;
EOF
    log "input.cfg erfolgreich erstellt."
else
    log "input.cfg existiert bereits."
fi

export MALLOC_TRIM_THRESHOLD_=131072
export MALLOC_MMAP_THRESHOLD_=131072
export MALLOC_ARENA_MAX=1
export WGPU_BACKEND=gl
export WGPU_SETTINGS_PRIO=webgl2
export SKATE3_NO_VSYNC=1
export SKATE3_NO_SHADOW=1
export SKATE3_RENDER_WIDTH=448
export SKATE3_RENDER_HEIGHT=336

log_sync ""
log_sync "== [11/12] gptokeyb =="
GPTOKEYB=""
for c in "$controlfolder/gptokeyb" /opt/system/Tools/PortMaster/gptokeyb \
         /usr/local/bin/gptokeyb /usr/bin/gptokeyb; do
    if [ -x "$c" ]; then GPTOKEYB="$c"; break; fi
done
GPTOKEYB_PID=""
if [ -n "$GPTOKEYB" ]; then
    "$GPTOKEYB" "$GAMEDIR/skate3" -k skate3 >/dev/null 2>&1 &
    GPTOKEYB_PID=$!
    log "gptokeyb gestartet (PID $GPTOKEYB_PID)"
fi

log_sync ""
log_sync "== [12/12] SKATE3 BINARY START =="
log_sync "  MemAvailable vor Start: $(grep MemAvailable /proc/meminfo 2>/dev/null | awk '{print $2 "kB"}' || echo '?')"
cd "$GAMEDIR" || { log_sync "FEHLER: cd $GAMEDIR"; exit 1; }

STDOUT="$GAMEDIR/skate3-stdout.txt"
STDERR="$GAMEDIR/skate3-stderr.txt"
: > "$STDOUT"
: > "$STDERR"

log_sync "[start] ./skate3 --assets \"$GAMEDIR/assets\""
STATUS=0
./skate3 --assets "$GAMEDIR/assets" "$@" > "$STDOUT" 2> "$STDERR" &
SKATE3_PID=$!
log_sync "[pid] skate3 PID=$SKATE3_PID"
wait "$SKATE3_PID" 2>/dev/null || STATUS=$?
log_sync "[done] skate3 exited (rc=$STATUS)"

if [ -n "$GPTOKEYB_PID" ] && kill -0 "$GPTOKEYB_PID" 2>/dev/null; then
    kill -TERM "$GPTOKEYB_PID" 2>/dev/null || true
    sleep 0.3
    kill -KILL "$GPTOKEYB_PID" 2>/dev/null || true
fi

if [ "$STATUS" -gt 128 ]; then
    SIG=$((STATUS - 128))
    case "$SIG" in
        4)  SIGNAME="SIGILL (falscher target-cpu?)" ;;
        6)  SIGNAME="SIGABRT" ;;
        7)  SIGNAME="SIGBUS" ;;
        8)  SIGNAME="SIGFPE" ;;
        9)  SIGNAME="SIGKILL (OOM-Killer?)" ;;
        11) SIGNAME="SIGSEGV (Segfault)" ;;
        15) SIGNAME="SIGTERM" ;;
        *)  SIGNAME="Signal $SIG" ;;
    esac
    log_sync "  -> $SIGNAME"
fi

if [ -d /tmp/Skate3RustEngine/CrashReports ]; then
    cp -f /tmp/Skate3RustEngine/CrashReports/*.txt "$GAMEDIR/" 2>/dev/null || true
    log "Crash-Report nach $GAMEDIR kopiert."
fi

log_sync ""
log_sync "== skate3-stderr.txt (letzte 80 Zeilen) =="
if [ -s "$STDERR" ]; then
    tail -80 "$STDERR" | while IFS= read -r line; do log "  $line"; done
else
    log "  (leer)"
fi
log_sync ""
log_sync "== skate3-stdout.txt (letzte 80 Zeilen) =="
if [ -s "$STDOUT" ]; then
    tail -80 "$STDOUT" | while IFS= read -r line; do log "  $line"; done
else
    log "  (leer)"
fi

log_sync ""
log_sync "=== Skate3 ENDE (rc=$STATUS) ==="
sync
exit "$STATUS"
'''

def prepare_portmaster():
    log("== portmaster/ ==")
    write_if_missing(PORTMASTER / "port.json", PORT_JSON)
    write_if_missing(PORTMASTER / "README.md", README_MD)
    write_if_missing(PORTMASTER / "gameinfo.xml", GAMEINFO_XML)
    launcher = PORTMASTER / "Skate3.sh"
    write_if_missing(launcher, LAUNCHER)
    if launcher.exists() and not CHECK_ONLY:
        try:
            os.chmod(launcher, 0o755)
        except OSError as e:
            warn(f"chmod auf {launcher.relative_to(ROOT)} fehlgeschlagen: {e}")


# ═════════════════════════════════════════════════════════════════════
# 9. wgpu [package]-Vererbung hart auflösen (via tomlkit)
# ═════════════════════════════════════════════════════════════════════
PACKAGE_INHERITABLE = {
    "authors", "categories", "description", "documentation",
    "edition", "exclude", "homepage", "include", "keywords",
    "license", "license-file", "publish", "readme", "repository",
    "rust-version", "version",
}

DEFAULTS = {
    "authors": '["gfx-rs developers"]',
    "edition": '"2021"',
    "license": '"MIT OR Apache-2.0"',
    "repository": '"https://github.com/gfx-rs/wgpu"',
    "version": '"27.0.4"',
    "rust-version": '"1.82"',
    "homepage": '"https://wgpu.rs/"',
}


def resolve_package_inheritance():
    log("== wgpu [package]-Vererbung hart auflösen ==")
    tomlkit = ensure_tomlkit()
    if tomlkit is None:
        fail("tomlkit fehlt – kann [package]-Vererbung nicht auflösen")
        return
    if not WGPU_ROOT.exists():
        warn(f"{WGPU_ROOT.relative_to(ROOT)} fehlt – wgpu nicht geklont?")
        return

    root_toml = WGPU_ROOT / "Cargo.toml"
    if not root_toml.exists():
        fail(f"{root_toml.relative_to(ROOT)} fehlt")
        return

    try:
        root_doc = tomlkit.parse(root_toml.read_text())
    except Exception as e:
        fail(f"wgpu/Cargo.toml nicht parsebar: {e}")
        return

    workspace_pkg = root_doc.get("workspace", {}).get("package", {})

    root_values = {}
    for k, v in workspace_pkg.items():
        if k in PACKAGE_INHERITABLE:
            root_values[k] = v
    for k, v in DEFAULTS.items():
        if k not in root_values:
            try:
                root_values[k] = tomlkit.parse(f"v = {v}")["v"]
            except Exception:
                pass

    if "workspace" not in root_doc:
        root_doc["workspace"] = tomlkit.table()
    if "package" not in root_doc["workspace"]:
        root_doc["workspace"]["package"] = tomlkit.table()
    missing_root = {k: v for k, v in root_values.items() if k not in workspace_pkg}
    if missing_root:
        for k, v in missing_root.items():
            root_doc["workspace"]["package"][k] = v
        if not CHECK_ONLY:
            root_toml.write_text(tomlkit.dumps(root_doc))
        log(f"  Root [workspace.package] ergänzt: {sorted(missing_root)}")

    total_files = 0
    total_fields = 0
    for cargo_toml in sorted(WGPU_ROOT.rglob("Cargo.toml")):
        if cargo_toml == root_toml:
            continue
        try:
            doc = tomlkit.parse(cargo_toml.read_text())
        except Exception as e:
            warn(f"  {cargo_toml.relative_to(ROOT)}: nicht parsebar: {e}")
            continue

        if "package" not in doc:
            continue

        pkg = doc["package"]
        modified = False
        for field in list(pkg.keys()):
            if not hasattr(pkg[field], "get"):
                continue
            if not (isinstance(pkg[field], dict) or hasattr(pkg[field], "get")):
                continue
            try:
                if pkg[field].get("workspace") is True:
                    if field in root_values:
                        pkg[field] = root_values[field]
                        modified = True
                        total_fields += 1
            except Exception:
                continue

        if modified:
            if not CHECK_ONLY:
                cargo_toml.write_text(tomlkit.dumps(doc))
            log(f"  {cargo_toml.relative_to(ROOT)}: package-Felder aufgelöst")
            total_files += 1

    log(f"  {total_files} Datei(en), {total_fields} Feld(er) aufgelöst")


# ═════════════════════════════════════════════════════════════════════
# 10. wgpu [dependencies]-Vererbung hart auflösen (via tomlkit)
# ═════════════════════════════════════════════════════════════════════
def adjust_path_for_vendored(dep_value, vendored_crate_dir, wgpu_root):
    if not isinstance(dep_value, dict):
        return dep_value
    if "path" not in dep_value:
        return dep_value
    try:
        original = str(dep_value["path"])
        abs_path = (wgpu_root / original).resolve()
        rel_path = os.path.relpath(abs_path, vendored_crate_dir.resolve())
        new_path = rel_path.replace(os.sep, "/")
        if not new_path.startswith("."):
            new_path = "./" + new_path
        dep_value["path"] = new_path
    except (ValueError, OSError):
        pass
    return dep_value


def merge_dep_spec(tomlkit, base, spec, vendored_dir, wgpu_root):
    spec_extra = {}
    for k in spec.keys():
        if k != "workspace":
            spec_extra[k] = spec[k]

    if isinstance(base, str):
        if spec_extra:
            result = tomlkit.inline_table()
            result["version"] = base
            for k, v in spec_extra.items():
                result[k] = v
            return adjust_path_for_vendored(result, vendored_dir, wgpu_root)
        else:
            return base
    else:
        result = tomlkit.inline_table()
        for k, v in base.items():
            result[k] = v
        for k, v in spec_extra.items():
            result[k] = v
        return adjust_path_for_vendored(result, vendored_dir, wgpu_root)


def resolve_dependency_inheritance():
    log("== wgpu [dependencies]-Vererbung hart auflösen (tomlkit) ==")
    tomlkit = ensure_tomlkit()
    if tomlkit is None:
        fail("tomlkit fehlt – kann [dependencies]-Vererbung nicht auflösen")
        return
    if not WGPU_ROOT.exists():
        warn(f"{WGPU_ROOT.relative_to(ROOT)} fehlt")
        return

    root_toml = WGPU_ROOT / "Cargo.toml"
    if not root_toml.exists():
        fail(f"{root_toml.relative_to(ROOT)} fehlt")
        return

    try:
        root_doc = tomlkit.parse(root_toml.read_text())
    except Exception as e:
        fail(f"wgpu/Cargo.toml nicht parsebar: {e}")
        return

    workspace_deps = root_doc.get("workspace", {}).get("dependencies", {})
    log(f"  Workspace-Dependencies: {len(workspace_deps)} Einträge")

    total_files = 0
    total_fields = 0
    missing = []

    for cargo_toml in sorted(WGPU_ROOT.rglob("Cargo.toml")):
        if cargo_toml == root_toml:
            continue
        try:
            doc = tomlkit.parse(cargo_toml.read_text())
        except Exception as e:
            warn(f"  {cargo_toml.relative_to(ROOT)}: nicht parsebar: {e}")
            continue

        vendored_dir = cargo_toml.parent
        modified = False

        sections = []
        for sec_name in ("dependencies", "dev-dependencies", "build-dependencies"):
            if sec_name in doc:
                sections.append((sec_name, doc[sec_name]))
        if "target" in doc:
            for target_name in doc["target"].keys():
                target_data = doc["target"][target_name]
                for sec_name in ("dependencies", "dev-dependencies", "build-dependencies"):
                    if sec_name in target_data:
                        sections.append((f"target.{target_name}.{sec_name}", target_data[sec_name]))

        for sec_path, section in sections:
            for dep_name in list(section.keys()):
                spec = section[dep_name]
                if not hasattr(spec, "get"):
                    continue
                if spec.get("workspace") is not True:
                    continue

                if dep_name not in workspace_deps:
                    missing.append((cargo_toml.relative_to(ROOT), dep_name))
                    continue

                base = workspace_deps[dep_name]
                try:
                    section[dep_name] = merge_dep_spec(
                        tomlkit, base, spec, vendored_dir, WGPU_ROOT
                    )
                    modified = True
                    total_fields += 1
                except Exception as e:
                    warn(f"  {cargo_toml.relative_to(ROOT)}: {dep_name}: {e}")

        if modified:
            if not CHECK_ONLY:
                try:
                    cargo_toml.write_text(tomlkit.dumps(doc))
                except Exception as e:
                    fail(f"  {cargo_toml.relative_to(ROOT)}: schreiben fehlgeschlagen: {e}")
                    continue
            log(f"  {cargo_toml.relative_to(ROOT)}: dependencies aufgelöst")
            total_files += 1

    if missing:
        for path, dep in missing:
            warn(f"  {path}: '{dep}' nicht in [workspace.dependencies]")
    log(f"  {total_files} Datei(en), {total_fields} Feld(er) aufgelöst")


def verify_dependency_inheritance():
    log("== wgpu [dependencies]-Vererbung verifizieren ==")
    tomlkit = ensure_tomlkit()
    if tomlkit is None or not WGPU_ROOT.exists():
        return
    root_toml = WGPU_ROOT / "Cargo.toml"
    if not root_toml.exists():
        return
    bad = []
    for cargo_toml in sorted(WGPU_ROOT.rglob("Cargo.toml")):
        if cargo_toml == root_toml:
            continue
        try:
            doc = tomlkit.parse(cargo_toml.read_text())
        except Exception as e:
            fail(f"  {cargo_toml.relative_to(ROOT)}: nicht parsebar nach Patch: {e}")
            continue

        sections = []
        for sec_name in ("dependencies", "dev-dependencies", "build-dependencies"):
            if sec_name in doc:
                sections.append(doc[sec_name])
        if "target" in doc:
            for target_name in doc["target"].keys():
                target_data = doc["target"][target_name]
                for sec_name in ("dependencies", "dev-dependencies", "build-dependencies"):
                    if sec_name in target_data:
                        sections.append(target_data[sec_name])

        for section in sections:
            for dep_name in section.keys():
                spec = section[dep_name]
                if hasattr(spec, "get") and spec.get("workspace") is True:
                    bad.append((cargo_toml.relative_to(ROOT), dep_name))

    if bad:
        for path, dep in bad:
            fail(f"  {path}: '{dep}' noch nicht aufgelöst")
    else:
        log("  OK: keine Dependency-Vererbung mehr vorhanden")


# ═════════════════════════════════════════════════════════════════════
# 11. wgpu-hal adapter.rs: Mali-G31 SSBO-Fix
# ═════════════════════════════════════════════════════════════════════
def patch_wgpu_adapter():
    log("== wgpu-hal adapter.rs (Mali-G31 SSBO-Fix) ==")
    adapter = WGPU_HAL / "src" / "gles" / "adapter.rs"
    if not adapter.exists():
        warn(f"{adapter.relative_to(ROOT)} nicht gefunden – SSBO-Fix übersprungen")
        return

    text = adapter.read_text()
    if "Mali-G31 R36S-Fix" in text:
        log("  bereits gepatcht")
        return

    pattern = re.compile(
        r'let\s+max_storage_buffers_per_shader_stage\s*=\s*'
        r'if\s+vertex_shader_storage_blocks\s*==\s*0\s*\{\s*'
        r'fragment_shader_storage_blocks\s*\}\s*else\s*\{\s*'
        r'vertex_shader_storage_blocks\s*\.\s*min\s*\(\s*fragment_shader_storage_blocks\s*\)\s*\}\s*;',
        re.DOTALL,
    )

    replacement = (
        "let max_storage_buffers_per_shader_stage = if vertex_shader_storage_blocks == 0 {\n"
        "            if vertex_ssbo_false_zero {\n"
        "                fragment_shader_storage_blocks\n"
        "            } else {\n"
        "                0 // Mali-G31 R36S-Fix\n"
        "            }\n"
        "        } else {\n"
        "            vertex_shader_storage_blocks.min(fragment_shader_storage_blocks)\n"
        "        };"
    )

    new_text, n = pattern.subn(replacement, text, count=1)
    if n == 0:
        fail("adapter.rs: Muster für max_storage_buffers_per_shader_stage nicht gefunden")
        for i, line in enumerate(text.splitlines(), 1):
            if "max_storage_buffers_per_shader_stage" in line:
                print(f"  Zeile {i}: {line!r}", file=sys.stderr)
        return

    if CHECK_ONLY:
        log("  würde patchen")
        return

    adapter.write_text(new_text)
    log("  gepatcht (Mali-G31 SSBO-Fix)")


# ═════════════════════════════════════════════════════════════════════
# 12. wgpu-hal/Cargo.toml: naga mit termcolor-Feature
# ═════════════════════════════════════════════════════════════════════
def patch_wgpu_hal_naga():
    log("== wgpu-hal naga termcolor-Feature ==")
    hal_cargo = WGPU_HAL / "Cargo.toml"
    if not hal_cargo.exists():
        warn(f"{hal_cargo.relative_to(ROOT)} fehlt – naga-Feature nicht gepatcht")
        return
    text = hal_cargo.read_text()
    old = "naga.workspace = true"
    new = 'naga = { workspace = true, features = ["termcolor"] }'
    if new in text:
        log("  bereits gepatcht")
        return
    if old not in text:
        if re.search(r'^naga\s*=\s*\{', text, re.MULTILINE):
            log("  bereits aufgelöst (naga = { ... }) – Feature prüfen")
            if "termcolor" in text:
                log("  termcolor bereits vorhanden")
                return
            def _add_termcolor(m):
                inner = m.group(1)
                mf = re.search(r'features\s*=\s*\[([^\]]*)\]', inner)
                if mf:
                    existing = mf.group(1).strip()
                    if '"termcolor"' in existing or "'termcolor'" in existing:
                        return m.group(0)
                    if existing:
                        new_feats = f'[{existing}, "termcolor"]'
                    else:
                        new_feats = '["termcolor"]'
                    inner_new = re.sub(r'features\s*=\s*\[[^\]]*\]',
                                       f'features = {new_feats}', inner)
                    return f"naga = {{{inner_new}}}"
                return f'naga = {{ {inner.rstrip()}, features = ["termcolor"] }}'
            new_text, n = re.subn(r'^naga\s*=\s*\{([^}]*)\}', _add_termcolor,
                                  text, count=1, flags=re.MULTILINE)
            if n > 0 and "termcolor" in new_text:
                if not CHECK_ONLY:
                    hal_cargo.write_text(new_text)
                log("  termcolor-Feature nachgerüstet")
                return
            fail("wgpu-hal/Cargo.toml: naga-Zeile gefunden, aber termcolor konnte nicht ergänzt werden")
            return
        fail("wgpu-hal/Cargo.toml: 'naga.workspace = true' nicht gefunden")
        return
    if CHECK_ONLY:
        log("  würde patchen")
        return
    hal_cargo.write_text(text.replace(old, new, 1))
    log("  gepatcht: naga mit termcolor-Feature")


# ═════════════════════════════════════════════════════════════════════
# 13. wgpu-hal Version verifizieren
# ═════════════════════════════════════════════════════════════════════
def verify_wgpu_hal_version(expected="27.0.4"):
    log("== wgpu-hal Version verifizieren ==")
    hal_toml = WGPU_HAL / "Cargo.toml"
    if not hal_toml.exists():
        fail(f"{hal_toml.relative_to(ROOT)} fehlt")
        return
    text = hal_toml.read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    actual = None
    if m:
        actual = m.group(1)
    elif "version.workspace = true" in text:
        root_toml = WGPU_ROOT / "Cargo.toml"
        if root_toml.exists():
            root_text = root_toml.read_text()
            m2 = re.search(
                r'\[workspace\.package\][^\[]*?version\s*=\s*"([^"]+)"',
                root_text, re.DOTALL,
            )
            if m2:
                actual = m2.group(1)
            else:
                fail("wgpu-hal/Cargo.toml: version.workspace = true, "
                     "aber [workspace.package].version nicht gefunden")
                return
        else:
            fail(f"{root_toml.relative_to(ROOT)} fehlt")
            return
    else:
        fail("wgpu-hal/Cargo.toml: kein version-Feld")
        return

    if actual != expected:
        fail(f"wgpu-hal hat Version '{actual}', erwartet '{expected}' "
             f"– [patch.crates-io] greift sonst nicht")
    else:
        log(f"  OK: wgpu-hal {actual}")


# ═════════════════════════════════════════════════════════════════════
# 14. crates/skate-game/src/input/platform.rs: unused XinputCaps-Import
# ═════════════════════════════════════════════════════════════════════
def patch_platform_unused_import():
    log("== crates/skate-game/src/input/platform.rs (unused XinputCaps) ==")
    if not PLATFORM_RS.exists():
        log("  nicht vorhanden – übersprungen")
        return
    text = PLATFORM_RS.read_text()

    if ("use super::controller_kind::ControllerKind;" in text
            and "XinputCaps" not in text):
        log("  bereits gepatcht")
        return

    patterns = [
        r'use\s+super::controller_kind::\{\s*ControllerKind\s*,\s*XinputCaps\s*,?\s*\}\s*;',
        r'use\s+super::controller_kind::\{\s*XinputCaps\s*,\s*ControllerKind\s*,?\s*\}\s*;',
    ]
    m = None
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            break

    if not m:
        log("  Muster 'use super::controller_kind::{..., XinputCaps}' "
            "nicht gefunden – nichts zu tun")
        return

    rest = text[:m.start()] + text[m.end():]
    rest_no_comments = re.sub(r'//[^\n]*', '', rest)
    if re.search(r'\bXinputCaps\b', rest_no_comments):
        log("  XinputCaps wird außerhalb der use-Zeile im Code verwendet – "
            "Import wird behalten (sonst Compile-Fehler)")
        return

    new_text = (text[:m.start()]
                + "use super::controller_kind::ControllerKind;"
                + text[m.end():])
    if CHECK_ONLY:
        log("  würde patchen (XinputCaps-Import entfernen)")
        return
    PLATFORM_RS.write_text(new_text)
    log("  gepatcht: XinputCaps-Import entfernt")


# ═════════════════════════════════════════════════════════════════════
# 15. crates/**/*.rs: .fetch_update( → .try_update( (Rust 1.99)
# ═════════════════════════════════════════════════════════════════════
def patch_fetch_update():
    log("== crates/**/*.rs: .fetch_update( → .try_update( ==")
    crates_dir = ROOT / "crates"
    if not crates_dir.exists():
        log("  crates/ nicht vorhanden – übersprungen")
        return

    total_files = 0
    total_calls = 0
    for rs in sorted(crates_dir.rglob("*.rs")):
        try:
            text = rs.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if ".fetch_update" not in text:
            continue
        new_text, n = re.subn(r'\.fetch_update\s*\(', '.try_update(', text)
        if n == 0:
            continue
        if CHECK_ONLY:
            log(f"  würde patchen: {rs.relative_to(ROOT)} ({n}×)")
        else:
            rs.write_text(new_text)
            log(f"  gepatcht: {rs.relative_to(ROOT)} ({n}×)")
        total_files += 1
        total_calls += n

    if total_files == 0:
        log("  keine .fetch_update(-Vorkommen gefunden")
    else:
        log(f"  {total_files} Datei(en), {total_calls} Aufruf(e) umgestellt")


# ═════════════════════════════════════════════════════════════════════
# 16. Cargo.lock entfernen
#
# Warum: Der vorhandene Cargo.lock hält wgpu 27.0.1 und naga 27.0.3
# von crates.io fest. Der Prepare-Skript patcht wgpu, wgpu-core,
# wgpu-types und naga auf vendor/wgpu (27.0.4), aber Cargo ignoriert
# die Patches, weil der Lock-File die crates.io-Versionen festschreibt.
# Ergebnis: Zwei Versionen von wgpu-hal und naga im Graph → E0277/E0271
# "trait not implemented" bzw. "type mismatch".
#
# Wenn Cargo.lock fehlt, löst Cargo die [patch.crates-io]-Sektionen
# neu auf und nimmt alle wgpu-Crates inkl. naga aus vendor/wgpu.
# ═════════════════════════════════════════════════════════════════════
def remove_cargo_lock():
    log("== Cargo.lock entfernen ==")
    lock = ROOT / "Cargo.lock"
    if not lock.exists():
        log("  keine Cargo.lock vorhanden")
        return
    if CHECK_ONLY:
        log("  würde löschen: Cargo.lock")
        return
    lock.unlink()
    log("  gelöscht: Cargo.lock (wird von Cargo neu erzeugt)")


# ═════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════
def main():
    global CHECK_ONLY, FORCE
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="nur prüfen, nichts schreiben")
    p.add_argument("--force", action="store_true", help="existierende Dateien überschreiben")
    args = p.parse_args()
    CHECK_ONLY = args.check
    FORCE = args.force

    log(f"Repo: {ROOT}")
    log(f"Modus: {'CHECK' if CHECK_ONLY else 'WRITE'}{' (FORCE)' if FORCE else ''}")
    log("")

    prepare_cargo_config()
    prepare_root_cargo()
    prepare_game_cargo()
    prepare_main()
    patch_main_allocator()
    prepare_app()
    prepare_fb0()
    prepare_headless()
    prepare_portmaster()
    patch_wgpu_hal_naga()
    resolve_package_inheritance()
    resolve_dependency_inheritance()
    verify_dependency_inheritance()
    patch_wgpu_adapter()
    verify_wgpu_hal_version()
    patch_platform_unused_import()
    patch_fetch_update()
    remove_cargo_lock()

    log("")
    log("== Zusammenfassung ==")
    log(f"  Fehler:    {len(errors)}")
    log(f"  Warnungen: {len(warnings)}")
    for w in warnings:
        log(f"    W: {w}")
    for e in errors:
        log(f"    E: {e}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
