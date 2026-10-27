#!/usr/bin/env python3
"""
aarch64-prepare.py — bereitet das Skate-3-Repo für RK3326/M9 Pro vor.

Wendet an:
  1. .cargo/config.toml (Cross-Toolchain, Cortex-A35-Flags)
  2. Root-Cargo.toml (libc; ALLE wgpu-* + naga als Pfad-Patches)
  3. skate-game/Cargo.toml (libc, default = [])
  4. main.rs (Module, WGPU-Env, HeadlessFbPlugin)
  5. app.rs (Backends::GL, primary_window: None, winit-disable,
             ScheduleRunnerPlugin, GPU-Preproc, ExitCondition)
  6. fb0_writer.rs (nur wenn fehlt)
  7. headless_plugin.rs (nur wenn fehlt)
  8. portmaster/ (nur wenn fehlt oder --force)
  9. vendor/wgpu: [package]-Vererbung hart auflösen
 10. vendor/wgpu: [dependencies]-Vererbung hart auflösen (via tomlkit)
 11. vendor/wgpu/wgpu-hal/Cargo.toml: naga mit termcolor-Feature
 12. vendor/wgpu/wgpu-hal/src/gles/adapter.rs (Mali-G31 SSBO-Fix)
 13. vendor/wgpu/wgpu-hal Version verifizieren (27.0.4)

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
PORTMASTER  = ROOT / "portmaster"
CARGO_CFG   = ROOT / ".cargo" / "config.toml"
WGPU_ROOT   = ROOT / "vendor" / "wgpu"
WGPU_HAL    = WGPU_ROOT / "wgpu-hal"

# Alle wgpu-Crates + naga, die auf den vendorten Workspace gepatcht werden
# müssen. Nur wgpu-hal zu patchen führt zu zwei wgpu-types-Versionen im
# Dependency-Graph (crates.io vs. vendored) → E0308-Typprobleme.
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
    """Importiert tomlkit; installiert es bei Bedarf via pip."""
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
    "-C", "link-arg=-static-libgcc",
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

    # libc ergänzen
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

    if "[patch.crates-io]" not in text:
        warn("Root-Cargo.toml: [patch.crates-io] fehlt")
        return text, changed

    # Alle wgpu-* + naga Pfad-Patches sicherstellen
    for crate_name, path in WGPU_PATCH_ENTRIES:
        active = f'{crate_name} = {{ path = "{path}" }}'
        commented = f'# {crate_name} = {{ path = "{path}" }}'
        if active in text:
            continue
        if commented in text:
            text = text.replace(commented, active, 1)
            changed = True
            continue
        # Nach dem [patch.crates-io]-Header einfügen
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
    if 'default = ["dev-dynamic"]' in text:
        text = text.replace('default = ["dev-dynamic"]', 'default = []', 1)
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
                '    std::env::set_var("WGPU_BACKEND", "gl");\n'
                '    std::env::set_var("WGPU_SETTINGS_PRIO", "webgl2");', 1)
            changed = True
        else:
            warn("main.rs: 'fn main() -> bevy::app::AppExit {' fehlt")

    if "headless_plugin::HeadlessFbPlugin" not in text:
        if "let mut app = app::build(config, manifest, graphs, physics, skater);" in text:
            text = text.replace(
                "let mut app = app::build(config, manifest, graphs, physics, skater);",
                "let mut app = app::build(config, manifest, graphs, physics, skater);\n"
                "    app.add_plugins(headless_plugin::HeadlessFbPlugin);", 1)
            changed = True
        else:
            warn("main.rs: app::build(...)-Anker fehlt")

    return text, changed

def prepare_main():
    log("== main.rs ==")
    patch_file(MAIN_RS, patch_main, "main.rs")


# ═════════════════════════════════════════════════════════════════════
# 5. app.rs
# ═════════════════════════════════════════════════════════════════════
def patch_app(text):
    changed = False

    if "Backends::VULKAN" in text:
        text = text.replace("Backends::VULKAN", "Backends::GL", 1)
        changed = True

    marker = "RenderCreation::Automatic(WgpuSettings"
    if marker in text:
        idx = text.find(marker)
        new_text = text[:idx] + "RenderCreation::Automatic(Box::new(WgpuSettings" + text[idx + len(marker):]
        auto_start = idx + len("RenderCreation::Automatic")
        if auto_start < len(new_text) and new_text[auto_start] == '(':
            close = find_matching(new_text, auto_start, '(', ')')
            if close > 0:
                new_text = new_text[:close] + ")" + new_text[close:]
                text = new_text
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

    # ExitCondition unabhängig vom primary_window-Zustand einfügen
    if "ExitCondition::DontExit" not in text:
        anchor = "primary_window: None"
        idx = text.find(anchor)
        if idx >= 0:
            insert_at = idx + len(anchor)
            j = insert_at
            while j < len(text) and text[j] in ' \t\n':
                j += 1
            if j < len(text) and text[j] == ',':
                text = text[:j+1] + "\n            exit_condition: ExitCondition::DontExit," + text[j+1:]
            else:
                text = text[:insert_at] + ",\n            exit_condition: ExitCondition::DontExit" + text[insert_at:]
            changed = True
        else:
            warn("app.rs: 'primary_window: None' fehlt – exit_condition nicht eingefügt")

    if "disable::<WinitPlugin>" not in text and "disable::<bevy::winit::WinitPlugin>" not in text:
        if ".build()" in text:
            pos = text.find(".build()")
            text = text[:pos] + ".disable::<bevy::winit::WinitPlugin>()\n        " + text[pos:]
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
        idx = text.find("fn build")
        if idx >= 0:
            brace_open = text.find("{", idx)
            if brace_open > 0:
                close = find_matching(text, brace_open, '{', '}')
                if close > 0:
                    insert = (
                        "\n    if let Some(render_app) = app.get_sub_app_mut(bevy::render::RenderApp) {\n"
                        "        render_app.insert_resource(bevy::render::batching::gpu_preprocessing::GpuPreprocessingSupport {\n"
                        "            max_supported_mode: bevy::render::batching::gpu_preprocessing::GpuPreprocessingMode::None,\n"
                        "        });\n"
                        "    }\n"
                    )
                    text = text[:close] + insert + text[close:]
                    changed = True
                else:
                    warn("app.rs: build()-Klammer nicht gefunden")
            else:
                warn("app.rs: build()-Öffnungsklammer nicht gefunden")

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
# 7. headless_plugin.rs (nur wenn fehlt)
# ═════════════════════════════════════════════════════════════════════
HEADLESS_RS_FALLBACK = r'''//! Headless-Rendering -> /dev/fb0 via Screenshot-API (Bevy 0.18).
use bevy::prelude::*;
use bevy::camera::{ImageRenderTarget, RenderTarget};
use bevy::math::FloatOrd;
use bevy::render::batching::gpu_preprocessing::NoIndirectDrawing;
use bevy::render::render_resource::{
    Extent3d, TextureDescriptor, TextureDimension, TextureFormat, TextureUsages,
};
use bevy::render::view::RenderLayers;
use bevy::render::view::window::screenshot::{
    Screenshot, ScreenshotCaptured, ScreenshotPlugin,
};
use bevy::ui::IsDefaultUiCamera;

use crate::fb0_writer::Fb0Writer;

pub struct HeadlessFbPlugin;

impl Plugin for HeadlessFbPlugin {
    fn build(&self, app: &mut App) {
        app.add_plugins(ScreenshotPlugin)
            .add_systems(Startup, (clear_fb0, setup_offscreen).chain())
            .add_systems(Update, (request_screenshot, copy_to_fb0).chain());
    }
}

#[derive(Resource)]
struct OffscreenTarget { handle: Handle<Image> }

#[derive(Resource, Default)]
struct FbState { writer: Option<Fb0Writer> }

fn clear_fb0(mut state: ResMut<FbState>) {
    if let Ok(mut w) = Fb0Writer::open() { let _ = w.clear(); }
    state.writer = Fb0Writer::open().ok();
}

fn setup_offscreen(
    mut commands: Commands,
    mut images: ResMut<Assets<Image>>,
    mut state: ResMut<FbState>,
) {
    let size = Extent3d { width: 448, height: 336, depth_or_array_layers: 1 };
    let mut image = Image {
        texture_descriptor: TextureDescriptor {
            label: None, size, dimension: TextureDimension::D2,
            format: TextureFormat::Rgba8UnormSrgb,
            mip_level_count: 1, sample_count: 1,
            usage: TextureUsages::TEXTURE_BINDING | TextureUsages::COPY_DST
                 | TextureUsages::COPY_SRC | TextureUsages::RENDER_ATTACHMENT,
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
            scale_factor: FloatOrd(1.0),
        }),
        NoIndirectDrawing,
        RenderLayers::default(),
        IsDefaultUiCamera,
    ));
    commands.insert_resource(OffscreenTarget { handle });
    if let Some(w) = &state.writer {
        info!("fb0: {}x{}, {} bpp", w.width(), w.height(), w.bits_per_pixel());
    } else {
        warn!("fb0 konnte nicht geoeffnet werden");
    }
}

fn request_screenshot(
    mut commands: Commands,
    camera: Query<Entity, With<Camera3d>>,
    target: Res<OffscreenTarget>,
) {
    if let Ok(entity) = camera.single() {
        commands.entity(entity).insert(Screenshot::image(target.handle.clone()));
    }
}

fn copy_to_fb0(
    mut messages: MessageReader<ScreenshotCaptured>,
    mut state: ResMut<FbState>,
) {
    for msg in messages.read() {
        let Some(writer) = state.writer.as_mut() else { continue; };
        let _ = writer.write_rgba8(&msg.image.data, msg.image.width(), msg.image.height());
    }
}
'''

def prepare_headless():
    log("== headless_plugin.rs ==")
    if HEADLESS_RS.exists():
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
    "desc": "Skate 3 native ARM64 port for RK3326 (M9 Pro / ArkOS). Requires your own Xbox 360 ISO to generate the .pak assets.",
    "desc_md": null,
    "inst": "Copy your converted .pak files into the skate3/assets/ folder.",
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

### You need your own game data
This port contains no game assets. Convert your own Xbox 360 ISO to `.pak` files.

1. Extract with `extract-xiso`.
2. Run `prepare_assets.py` from the engine repo.
3. Run `vita-bake` to produce `.pak` files.
4. Copy `.pak` files into `skate3/assets/`.

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
set -euo pipefail
XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
GAMEDIR="$SCRIPT_DIR/skate3"
if [ ! -x "$GAMEDIR/skate3" ]; then
    echo "FEHLER: $GAMEDIR/skate3 fehlt." >&2
    exit 1
fi
controlfolder=""
for candidate in "/opt/system/Tools/PortMaster" "/opt/tools/PortMaster" "$XDG_DATA_HOME/PortMaster" "/roms/ports/PortMaster" "/roms/tools/PortMaster"; do
    if [ -d "$candidate" ]; then controlfolder="$candidate"; break; fi
done
if [ -n "$controlfolder" ] && [ -f "$controlfolder/control.txt" ]; then
    source "$controlfolder/control.txt" 2>/dev/null || true
    if [ -f "${controlfolder}/mod_${CFW_NAME}.txt" ]; then
        source "${controlfolder}/mod_${CFW_NAME}.txt" 2>/dev/null || true
    fi
fi
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-kmsdrm}"
unset SDL_AUDIODRIVER AUDIODEV
if [ -n "${sdl_controllerconfig:-}" ]; then
    export SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"
fi
if [ -n "${SDL_GAMECONTROLLERCONFIG:-}" ]; then
    export SDL_GAMECONTROLLERCONFIG_FILE="/tmp/skate3-gamecontrollerdb.txt"
    printf '%s\n' "$SDL_GAMECONTROLLERCONFIG" > "$SDL_GAMECONTROLLERCONFIG_FILE" 2>/dev/null || true
fi
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
    rm -rf /tmp/skate3-mali
    mkdir -p /tmp/skate3-mali
    for name in libmali.so.0 libmali.so.1 libmali.so libEGL.so.1 libEGL.so libGLESv2.so.2 libGLESv2.so; do
        cp -f "$SYSTEM_MALI" "/tmp/skate3-mali/$name"
    done
    export LD_LIBRARY_PATH="/tmp/skate3-mali:$GAMEDIR:/usr/lib/aarch64-linux-gnu:${LD_LIBRARY_PATH:-}"
else
    export LD_LIBRARY_PATH="$GAMEDIR:/usr/lib/aarch64-linux-gnu:${LD_LIBRARY_PATH:-}"
fi
export MALLOC_TRIM_THRESHOLD_=131072
export MALLOC_MMAP_THRESHOLD_=131072
export MALLOC_ARENA_MAX=2
export WGPU_BACKEND=gl
export WGPU_SETTINGS_PRIO=webgl2
export SKATE3_NO_VSYNC=1
export SKATE3_NO_SHADOW=1
export SKATE3_RENDER_WIDTH=448
export SKATE3_RENDER_HEIGHT=336
cpu_gov=""
for c in /sys/devices/system/cpu/cpufreq/policy0/scaling_governor \
         /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor; do
    if [ -w "$c" ]; then cpu_gov="$c"; break; fi
done
if [ -n "$cpu_gov" ]; then echo performance > "$cpu_gov" 2>/dev/null || true; fi
GPTOKEYB=""
for c in "$controlfolder/gptokeyb" /opt/system/Tools/PortMaster/gptokeyb \
         /usr/local/bin/gptokeyb /usr/bin/gptokeyb; do
    if [ -x "$c" ]; then GPTOKEYB="$c"; break; fi
done
GPTOKEYB_PID=""
if [ -n "$GPTOKEYB" ]; then
    "$GPTOKEYB" "$GAMEDIR/skate3" -k skate3 &
    GPTOKEYB_PID=$!
fi
cd "$GAMEDIR"
STATUS=0
./skate3 "$@" || STATUS=$?
if [ -n "$GPTOKEYB_PID" ]; then kill -TERM "$GPTOKEYB_PID" 2>/dev/null || true; fi
if [ -n "$cpu_gov" ]; then echo ondemand > "$cpu_gov" 2>/dev/null || true; fi
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

    # Konkrete Werte aus root, mit Defaults für bekannte Felder
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

    # Root ergänzen
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

    # Jede vendorte Cargo.toml durchgehen
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
    """Passe path = "..." in dep_value so an, dass er relativ zum vendorten
    Crate korrekt aufgelöst wird. dep_value ist ein dict."""
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
    """Führt base (aus workspace.dependencies) und spec (aus crate) zusammen.
    spec enthält 'workspace': true, das entfernt wird."""
    # spec ohne 'workspace' extrahieren
    spec_extra = {}
    for k in spec.keys():
        if k != "workspace":
            spec_extra[k] = spec[k]

    # base kann ein String (Version) oder ein dict (Tabelle) sein
    if isinstance(base, str):
        if spec_extra:
            # Tabelle bauen: version + extras
            result = tomlkit.inline_table()
            result["version"] = base
            for k, v in spec_extra.items():
                result[k] = v
            return adjust_path_for_vendored(result, vendored_dir, wgpu_root)
        else:
            return base
    else:
        # base ist dict (tomlkit Table oder InlineTable)
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

        # Alle dependency-Sektionen: dependencies, dev-dependencies, build-dependencies
        # und target.*.dependencies
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
                # Nur inline tables die 'workspace = true' haben
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
        # Prüfen, ob resolve_dependency_inheritance() bereits gelaufen ist
        # und die Zeile schon aufgelöst hat.
        if re.search(r'^naga\s*=\s*\{', text, re.MULTILINE):
            log("  bereits aufgelöst (naga = { ... }) – Feature prüfen")
            if "termcolor" in text:
                log("  termcolor bereits vorhanden")
                return
            # Versuche, termcolor nachzurüsten (einfacher Fall)
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
    prepare_app()
    prepare_fb0()
    prepare_headless()
    prepare_portmaster()
    # naga termcolor MUSS vor resolve_dependency_inheritance() laufen,
    # solange in wgpu-hal/Cargo.toml noch "naga.workspace = true" steht.
    patch_wgpu_hal_naga()
    resolve_package_inheritance()
    resolve_dependency_inheritance()
    verify_dependency_inheritance()
    patch_wgpu_adapter()
    verify_wgpu_hal_version()

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
