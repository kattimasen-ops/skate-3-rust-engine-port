//! /dev/fb0-Writer für RK3326 / M9 Pro.
//! Unterstützt RGB565 (16bpp) und XRGB8888/ARGB8888 (32bpp).

use std::fs::{File, OpenOptions};
use std::io;
use std::os::unix::io::AsRawFd;
use std::ptr;

#[repr(C)]
#[derive(Default)]
struct FbBitfield {
    offset: u32,
    length: u32,
    msb_right: u32,
}

#[repr(C)]
#[derive(Default)]
struct FbVarScreeninfo {
    xres: u32,
    yres: u32,
    xres_virtual: u32,
    yres_virtual: u32,
    xoffset: u32,
    yoffset: u32,
    bits_per_pixel: u32,
    grayscale: u32,
    red: FbBitfield,
    green: FbBitfield,
    blue: FbBitfield,
    transp: FbBitfield,
    nonstd: u32,
    activate: u32,
    height: u32,
    width: u32,
    accel_flags: u32,
    pixclock: u32,
    left_margin: u32,
    right_margin: u32,
    upper_margin: u32,
    lower_margin: u32,
    hsync_len: u32,
    vsync_len: u32,
    sync: u32,
    vmode: u32,
    rotate: u32,
    colorspace: u32,
    reserved: [u32; 4],
}

#[repr(C)]
#[derive(Default)]
struct FbFixScreeninfo {
    id: [u8; 16],
    smem_start: usize,
    smem_len: u32,
    type_: u32,
    type_aux: u32,
    visual: u32,
    xpanstep: u16,
    ypanstep: u16,
    ywrapstep: u16,
    line_length: u32,
    mmio_start: usize,
    mmio_len: u32,
    accel: u32,
    capabilities: u16,
    reserved: [u16; 2],
}

const FBIOGET_VSCREENINFO: u64 = 0x4600;
const FBIOGET_FSCREENINFO: u64 = 0x4602;

pub struct Fb0Writer {
    file: File,
    var: FbVarScreeninfo,
    fix: FbFixScreeninfo,
    mmap_ptr: *mut u8,
    mmap_len: usize,
    bytes_per_pixel: usize,
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
            libc::mmap(
                ptr::null_mut(),
                mmap_len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                fd,
                0,
            )
        };
        if mmap_ptr == libc::MAP_FAILED {
            return Err(io::Error::last_os_error());
        }

        Ok(Self {
            file,
            var,
            fix,
            mmap_ptr: mmap_ptr as *mut u8,
            mmap_len,
            bytes_per_pixel: (var.bits_per_pixel / 8) as usize,
        })
    }

    pub fn width(&self) -> u32 { self.var.xres }
    pub fn height(&self) -> u32 { self.var.yres }
    pub fn bits_per_pixel(&self) -> u32 { self.var.bits_per_pixel }

    pub fn write_rgba8(&mut self, rgba: &[u8], width: u32, height: u32) -> io::Result<()> {
        if rgba.len() < (width * height * 4) as usize {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "RGBA-Puffer zu klein"));
        }
        let line_len = self.fix.line_length as usize;
        let _bpp = self.bytes_per_pixel;

        match self.var.bits_per_pixel {
            32 => {
                for y in 0..height as usize {
                    let src_row = &rgba[y * width as usize * 4..];
                    let dst_row = unsafe { self.mmap_ptr.add(y * line_len) as *mut u32 };
                    for x in 0..width as usize {
                        let r = src_row[x * 4 + 0] as u32;
                        let g = src_row[x * 4 + 1] as u32;
                        let b = src_row[x * 4 + 2] as u32;
                        unsafe { *dst_row.add(x) = (r << 16) | (g << 8) | b; }
                    }
                }
            }
            16 => {
                for y in 0..height as usize {
                    let src_row = &rgba[y * width as usize * 4..];
                    let dst_row = unsafe { self.mmap_ptr.add(y * line_len) as *mut u16 };
                    for x in 0..width as usize {
                        let r = (src_row[x * 4 + 0] >> 3) as u16;
                        let g = (src_row[x * 4 + 1] >> 2) as u16;
                        let b = (src_row[x * 4 + 2] >> 3) as u16;
                        unsafe { *dst_row.add(x) = (r << 11) | (g << 5) | b; }
                    }
                }
            }
            other => {
                return Err(io::Error::new(
                    io::ErrorKind::Unsupported,
                    format!("Framebuffer mit {other} bpp wird nicht unterstützt"),
                ));
            }
        }
        let _ = &self.file;
        Ok(())
    }
}

impl Drop for Fb0Writer {
    fn drop(&mut self) {
        unsafe {
            libc::munmap(self.mmap_ptr as *mut _, self.mmap_len);
        }
    }
}
