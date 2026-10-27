//! Headless-Rendering -> /dev/fb0 für RK3326 / M9 Pro.
//!
//! Quellen:
//! - R36S-Blog: https://blog.csdn.net/2601_96158891/article/details/163443328
//! - Bevy-Headless-UI-Fix: https://jishuzhan.net/article/2092119687447105537
//! - Mighty-Rodent-fb0: https://gitcode.com/k4m7v2pz/rust-bevy-mighty-rodent

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
struct OffscreenTarget {
    handle: Handle<Image>,
}

#[derive(Resource, Default)]
struct FbState {
    writer: Option<Fb0Writer>,
}

/// Einmaliges Schwarzschreiben beim Start (R36S-Erkenntnis).
/// Verhindert, dass der letzte Frame des vorherigen Spiels sichtbar bleibt.
fn clear_fb0(mut state: ResMut<FbState>) {
    if let Ok(mut w) = Fb0Writer::open() {
        let width = w.width();
        let height = w.height();
        let black = vec![0u8; (width * height * 4) as usize];
        let _ = w.write_rgba8(&black, width, height);
        info!("fb0: Startbildschirm auf Schwarz gesetzt");
    }
    state.writer = Fb0Writer::open().ok();
}

fn setup_offscreen(
    mut commands: Commands,
    mut images: ResMut<Assets<Image>>,
    mut state: ResMut<FbState>,
) {
    let size = Extent3d {
        width: 448,
        height: 336,
        depth_or_array_layers: 1,
    };
    let mut image = Image {
        texture_descriptor: TextureDescriptor {
            label: None,
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
            scale_factor: FloatOrd(1.0),
        }),
        NoIndirectDrawing,
        RenderLayers::default(),
        // PFLICHT: Sonst filtert DefaultUiCamera::get() Image-Targets heraus
        // und die gesamte UI bleibt unsichtbar (kein Fehler, kein Warn).
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
        let _ = writer.write_rgba8(
            &msg.image.data,
            msg.image.width(),
            msg.image.height(),
        );
    }
}
