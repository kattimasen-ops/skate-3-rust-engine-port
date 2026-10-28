//! Headless-Rendering -> /dev/fb0 via ImageCopyPlugin (asynchroner Readback).
//!
//! R36S-Bevy0.18-ImageCopy: Nutzt ImageCopyPlugin + persistenten Buffer
//! + crossbeam-Channel statt ScreenshotPlugin (synchroner Readback = Stall).
//!
//! Der RenderGraph-Node `ImageCopy` kopiert die Offscreen-Textur in einen
//! MAP_READ-Buffer. `receive_image_from_buffer` läuft nach dem Render-Schedule
//! im RenderApp und schickt die fertigen Bytes über einen crossbeam-Channel
//! an die Main World. `write_to_fb0` läuft in PostUpdate und schreibt die
//! Bytes nach /dev/fb0.
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
