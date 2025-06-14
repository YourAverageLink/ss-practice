use core::ffi::c_void;

use crate::system::math::{Vec3f, Vec3s};

#[repr(C)]
pub struct ActorLink {
    pub base_base:      [u8; 0x60 - 0x00],
    pub vtable:         u32,
    pub obj_base_pad0:  [u8; 0x54],
    pub angle:          Vec3s,
    pub pad:            [u8; 2],
    pub pos:            Vec3f,
    pub obj_base_pad:   [u8; 0x144 - (0x64 + 0x5C + 0xC)],
    pub forward_speed:   f32,
    pub forward_accel:   f32,
    pub forward_max_speed: f32,
    pub velocity:       Vec3f,
    pub pad01:          [u8; 0x4498 - 0x15C],
    pub stamina_amount: u32,
    // More after
}
extern "C" {
    static LINK_PTR: *mut ActorLink;
    fn checkXZDistanceFromLink(actor: *const c_void, distance: f32) -> bool;
}

pub fn as_mut() -> Option<&'static mut ActorLink> {
    unsafe { LINK_PTR.as_mut() }
}
