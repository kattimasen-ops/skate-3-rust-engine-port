#!/usr/bin/env python3
"""
aarch64-prepare.py — bereitet das Skate-3-Repo für RK3326/M9 Pro vor.

Idempotent: bereits gepatchte Dateien werden übersprungen.
Alle Patches werden geloggt; Fehlschläge kritischer Patches -> Exit 1.

Aufruf:
    python3 tools/aarch64-prepare.py
    python3 tools/aarch64-prepare.py --check    # nur prüfen, nicht schreiben
    python3 tools/aarch64-prepare.py --force    # existierende Dateien überschreiben
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path.cwd()
GAME_SRC   = ROOT / "crates" / "skate-game" / "src"
GAME_CARGO = ROOT / "crates" / "skate-game" / "Cargo.toml"
ROOT_CARGO = ROOT / "Cargo.toml"
MAIN_RS    = GAME_SRC / "main.rs"
APP_RS     = GAME_SRC / "app.rs"
FB0_RS     = GAME_SRC / "fb0_writer.rs"
HEADLESS_RS = GAME_SRC / "headless_plugin.rs"
PORTMASTER = ROOT / "portmaster"
CARGO_CFG  = ROOT / ".cargo" / "config.toml"

CHECK_ONLY = False
FORCE = False
notes = []
warnings = []
errors = []

def log(msg): print(f"[prepare] {msg}")
def warn(msg):
    warnings.append(msg)
    print(f"[prepare] WARNUNG: {msg}")
def fail(msg):
    errors.append(msg)
    print(f"[prepare] FEHLER: {msg}", file=sys.stderr)

# ─────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────

def find_matching(text, open_idx, open_char='{', close_char='}'):
    """Index der passenden schließenden Klammer oder -1."""
    if text[open_idx] != open_char:
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

def write_file(path, content, marker=None):
    """Schreibt Datei, wenn sie fehlt oder --force. Bei marker: nur überschreiben, wenn Marker fehlt."""
    if path.exists() and not FORCE:
        if marker and marker not in path.read_text():
            log(f"überschrieben (Marker '{marker}' fehlt): {path}")
        else:
            log(f"übersprungen (existiert): {path}")
            return False
    if CHECK_ONLY:
        log(f"würde schreiben: {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    log(f"geschrieben: {path}")
    return True

def patch_file(path, patch_fn, description):
    """Wendet patch_fn auf path an. patch_fn gibt (new_text, changed: bool) zurück."""
    if not path.exists():
        fail(f"{path} nicht gefunden ({description})")
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

# ─────────────────────────────────────────────────────────────────────
# 1. .cargo/config.toml
# ─────────────────────────────────────────────────────────────────────

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
    write_file(CARGO_CFG, CARGO_CONFIG)

# ─────────────────────────────────────────────────────────────────────
# 2. Root-Cargo.toml: libc + wgpu-hal-Patch
# ─────────────────────────────────────────────────────────────────────

def patch_root_cargo(text):
    changed = False
    if 'libc = "0.2"' not in text and "libc.workspace" not in text:
        if 'serde_json = "1.0"' in text:
            text = text.replace('serde_json = "1.0"',
                                'serde_json = "1.0"\nlibc = "0.2"', 1)
        elif "[workspace.dependencies]" in text:
            text = text.replace("[workspace.dependencies]",
                                '[workspace.dependencies]\nlibc = "0.2"', 1)
        else:
            fail("Root-Cargo.toml: kein Anker für libc gefunden")
            return text, False
        changed = True

    # wgpu-hal-Patch aktivieren
    if '# wgpu-hal = { path = "vendor/wgpu/wgpu-hal" }' in text:
        text = text.replace('# wgpu-hal = { path = "vendor/wgpu/wgpu-hal" }',
                            'wgpu-hal = { path = "vendor/wgpu/wgpu-hal" }', 1)
        changed = True
    elif 'wgpu-hal = { path = "vendor/wgpu/wgpu-hal" }' not in text:
        if "[patch.crates-io]" in text:
            text = text.replace("[patch.crates-io]",
                                '[patch.crates-io]\nwgpu-hal = { path = "vendor/wgpu/wgpu-hal" }', 1)
            changed = True
        else:
            warn("Root-Cargo.toml: [patch.crates-io] fehlt")
    return text, changed

def prepare_root_cargo():
    log("== Root-Cargo.toml ==")
    patch_file(ROOT_CARGO, patch_root_cargo, "root Cargo.toml")

# ─────────────────────────────────────────────────────────────────────
# 3. skate-game/Cargo.toml: libc
# ─────────────────────────────────────────────────────────────────────

def patch_game_cargo(text):
    if "libc.workspace" in text or 'libc = "0.2"' in text:
        return text, False
    if "[dependencies]" in text:
        text = text.replace("[dependencies]",
                            "[dependencies]\nlibc.workspace = true", 1)
    else:
        text += "\n[dependencies]\nlibc.workspace = true\n"
    return text, True

def prepare_game_cargo():
    log("== skate-game/Cargo.toml ==")
    if not GAME_CARGO.exists():
        fail(f"{GAME_CARGO} nicht gefunden")
        return
    patch_file(GAME_CARGO, patch_game_cargo, "skate-game Cargo.toml")

# ─────────────────────────────────────────────────────────────────────
# 4. main.rs: Module, Env-Vars, Plugin
# ─────────────────────────────────────────────────────────────────────

def patch_main(text):
    changed = False

    # 4a. Module
    if "mod fb0_writer;" not in text:
        if "mod trigger_volumes;" in text:
            text = text.replace(
                "mod trigger_volumes;",
                "mod trigger_volumes;\nmod fb0_writer;\nmod headless_plugin;", 1)
            changed = True
        else:
            warn("main.rs: 'mod trigger_volumes;' fehlt – Module nicht eingefügt")
            warn("        Manuell einfügen: 'mod fb0_writer; mod headless_plugin;'")

    # 4b. WGPU-Env
    if 'std::env::set_var("WGPU_BACKEND"' not in text:
        if "fn main() -> bevy::app::AppExit {" in text:
            text = text.replace(
                "fn main() -> bevy::app::AppExit {",
                'fn main() -> bevy::app::AppExit {\n'
                '    std::env::set_var("WGPU_BACKEND", "gl");\n'
                '    std::env::set_var("WGPU_SETTINGS_PRIO", "webgl2");', 1)
            changed = True
        else:
            warn("main.rs: 'fn main() -> bevy::app::AppExit {' fehlt – Env nicht eingefügt")

    # 4c. Plugin
    if "headless_plugin::HeadlessFbPlugin" not in text:
        if "let mut app = app::build(config, manifest, graphs, physics, skater);" in text:
            text = text.replace(
                "let mut app = app::build(config, manifest, graphs, physics, skater);",
                "let mut app = app::build(config, manifest, graphs, physics, skater);\n"
                "    app.add_plugins(headless_plugin::HeadlessFbPlugin);", 1)
            changed = True
        else:
            warn("main.rs: app::build(...)-Anker fehlt – Plugin nicht eingefügt")

    return text, changed

def prepare_main():
    log("== main.rs ==")
    patch_file(MAIN_RS, patch_main, "main.rs")

# ─────────────────────────────────────────────────────────────────────
# 5. app.rs: Backends, Box, primary_window, exit_condition, winit, GPU-Preproc
# ─────────────────────────────────────────────────────────────────────

def patch_app(text):
    changed = False

    # 5a. Backends::VULKAN -> GL
    if "Backends::VULKAN" in text:
        text = text.replace("Backends::VULKAN", "Backends::GL", 1)
        changed = True

    # 5b. RenderCreation::Automatic(WgpuSettings { ... }) -> Box::new(...)
    marker = "RenderCreation::Automatic(WgpuSettings"
    if marker in text:
        idx = text.find(marker)
        # Ersetze den Anfang
        new_text = (text[:idx]
                    + "RenderCreation::Automatic(Box::new(WgpuSettings"
                    + text[idx + len(marker):])
        # Finde die passende schließende Klammer von Automatic(
        auto_start = idx + len("RenderCreation::Automatic")
        if auto_start < len(new_text) and new_text[auto_start] == '(':
            close = find_matching(new_text, auto_start, '(', ')')
            if close > 0:
                # Füge ")" vor der schließenden Klammer ein
                new_text = new_text[:close] + ")" + new_text[close:]
                text = new_text
                changed = True
            else:
                warn("app.rs: Automatic(-Klammer nicht gefunden")

    # 5c. primary_window: Some(Window { ... }) -> primary_window: None
    if "primary_window: Some" in text and "primary_window: None" not in text:
        idx = text.find("primary_window: Some")
        some_end = idx + len("primary_window: Some")
        # Springe über Whitespace zur '('
        j = some_end
        while j < len(text) and text[j] in ' \t':
            j += 1
        if j < len(text) and text[j] == '(':
            close = find_matching(text, j, '(', ')')
            if close > 0:
                text = text[:idx] + "primary_window: None" + text[close + 1:]
                changed = True
            else:
                warn("app.rs: primary_window-Some-Klammer nicht gefunden")

    # 5d. exit_condition: ExitCondition::DontExit
    if "primary_window: None" in text and "ExitCondition::DontExit" not in text:
        text = text.replace(
            "primary_window: None",
            "primary_window: None,\n            exit_condition: ExitCondition::DontExit",
            1)
        changed = True

    # 5e. .disable::<WinitPlugin>() vor .build()
    if "disable::<WinitPlugin>" not in text and "disable::<bevy::winit::WinitPlugin>" not in text:
        if ".build()" in text:
            pos = text.find(".build()")
            text = (text[:pos]
                    + ".disable::<bevy::winit::WinitPlugin>()\n        "
                    + text[pos:])
            changed = True
        else:
            warn("app.rs: '.build()' fehlt – .disable::<WinitPlugin>() nicht eingefügt")

    # 5f. GpuPreprocessingSupport am Ende von build()
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
        fail(f"{APP_RS} nicht gefunden – app.rs-Patch kritisch")
        return
    patch_file(APP_RS, patch_app, "app.rs")

# ─────────────────────────────────────────────────────────────────────
# 6. fb0_writer.rs (nur wenn fehlt)
# ─────────────────────────────────────────────────────────────────────

FB0_RS_CONTENT = r'''//! /dev/fb0-Writer für RK3326 / M9 Pro.
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
    write_file(FB0_RS, FB0_RS_CONTENT)

# ─────────────────────────────────────────────────────────────────────
# 7. headless_plugin.rs (nur wenn fehlt)
# ─────────────────────────────────────────────────────────────────────

HEADLESS_RS_CONTENT = r'''//! Headless-Rendering -> /dev/fb0 via Screenshot-API (Bevy 0.18).
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
    if let Ok(mut w) = Fb0Writer::open() {
        let _ = w.clear();
    }
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
        warn!("fb0 konnte nicht geoeffnet werden; Ausgabe nur ueber Log");
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
    write_file(HEADLESS_RS, HEADLESS_RS_CONTENT)

# ─────────────────────────────────────────────────────────────────────
# 8. portmaster/
# ─────────────────────────────────────────────────────────────────────

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

# Mali-Libs: FAT32 unterstuetzt keine Symlinks -> cp -f
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
    write_file(PORTMASTER / "port.json", PORT_JSON)
    write_file(PORTMASTER / "README.md", README_MD)
    write_file(PORTMASTER / "gameinfo.xml", GAMEINFO_XML)
    launcher = PORTMASTER / "Skate3.sh"
    write_file(launcher, LAUNCHER)
    if launcher.exists() and not CHECK_ONLY:
        os.chmod(launcher, 0o755)

# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

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

    log("")
    log("== Zusammenfassung ==")
    log(f"  Fehler:   {len(errors)}")
    log(f"  Warnungen: {len(warnings)}")
    for w in warnings:
        log(f"    W: {w}")
    for e in errors:
        log(f"    E: {e}")

    if errors:
        log("Abbruch wegen kritischer Fehler.")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
