/* SPDX-License-Identifier: MIT
 * CPU-only DRM scanout. No fonts, GL, or glyph generation lives here. */
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/kd.h>
#include <linux/vt.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

typedef struct { uint32_t handle, fb, pitch; uint64_t size; uint8_t *map; } buffer;
typedef struct {
    int fd, tty, old_kd, active, front, configured;
    uint32_t connector, crtc;
    drmModeModeInfo mode;
    drmModeCrtc *saved;
    uint32_t *saved_connectors;
    int saved_count;
    struct vt_mode old_vt;
    buffer buffers[2];
} nd_kms;

static int modeset(nd_kms *k, uint32_t fb) {
    return drmModeSetCrtc(k->fd, k->crtc, fb, 0, 0, &k->connector, 1, &k->mode);
}

void nd_kms_close(nd_kms *k) {
    if (!k) return;
    if (k->fd >= 0) {
        if (k->active && k->configured && k->saved) {
            drmModeSetCrtc(k->fd, k->saved->crtc_id, k->saved->buffer_id,
                          k->saved->x, k->saved->y, k->saved_connectors,
                          k->saved_count, k->saved->mode_valid ? &k->saved->mode : NULL);
        }
        for (int i = 0; i < 2; i++) {
            buffer *b = &k->buffers[i];
            if (b->map && b->map != MAP_FAILED) munmap(b->map, b->size);
            if (b->fb) drmModeRmFB(k->fd, b->fb);
            if (b->handle) {
                struct drm_mode_destroy_dumb destroy = {.handle = b->handle};
                drmIoctl(k->fd, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy);
            }
        }
        drmDropMaster(k->fd);
        close(k->fd);
    }
    if (k->saved) drmModeFreeCrtc(k->saved);
    free(k->saved_connectors);
    if (k->tty >= 0) {
        ioctl(k->tty, KDSETMODE, k->old_kd);
        ioctl(k->tty, VT_SETMODE, &k->old_vt);
        close(k->tty);
    }
    free(k);
}

nd_kms *nd_kms_open(const char *card, int width, int height, int *geometry) {
    nd_kms *k = calloc(1, sizeof(*k));
    if (!k) return NULL;
    k->fd = k->tty = -1;
    /* Only the already-active controlling VT may be taken. Never switch a
       user's desktop or grab a different terminal as a side effect. */
    int tty = open("/dev/tty", O_RDWR | O_CLOEXEC);
    struct vt_stat state;
    int number = 0;
    if (tty < 0 || ioctl(tty, VT_GETSTATE, &state) || ioctl(tty, TIOCGDEV, &number)
        || (number & 255) != state.v_active || ioctl(tty, KDGETMODE, &k->old_kd)
        || k->old_kd != KD_TEXT || ioctl(tty, VT_GETMODE, &k->old_vt)) {
        if (tty >= 0) close(tty);
        errno = EBUSY; nd_kms_close(k); return NULL;
    }
    k->tty = tty;
    k->fd = open(card, O_RDWR | O_CLOEXEC);
    uint64_t dumb = 0;
    if (k->fd < 0 || drmGetCap(k->fd, DRM_CAP_DUMB_BUFFER, &dumb) || !dumb || drmSetMaster(k->fd)) goto fail;
    k->active = 1;
    drmModeRes *res = drmModeGetResources(k->fd);
    if (!res) goto fail;
    for (int i = 0; i < res->count_connectors && !k->connector; i++) {
        drmModeConnector *c = drmModeGetConnector(k->fd, res->connectors[i]);
        if (!c) continue;
        if (c->connection == DRM_MODE_CONNECTED && c->count_modes) {
            int mode = -1;
            for (int m = 0; m < c->count_modes; m++) {
                if (width && height) {
                    if (c->modes[m].hdisplay == width && c->modes[m].vdisplay == height) { mode = m; break; }
                } else if (mode < 0 || (c->modes[m].type & DRM_MODE_TYPE_PREFERRED)) mode = m;
            }
            for (int e = 0; e < c->count_encoders && mode >= 0 && !k->crtc; e++) {
                drmModeEncoder *encoder = drmModeGetEncoder(k->fd, c->encoders[e]);
                if (!encoder) continue;
                if (encoder->crtc_id) k->crtc = encoder->crtc_id;
                else for (int j = 0; j < res->count_crtcs; j++)
                    if (encoder->possible_crtcs & (1u << j)) { k->crtc = res->crtcs[j]; break; }
                drmModeFreeEncoder(encoder);
            }
            if (k->crtc && mode >= 0) { k->connector = c->connector_id; k->mode = c->modes[mode]; }
        }
        drmModeFreeConnector(c);
    }
    if (k->connector) {
        k->saved = drmModeGetCrtc(k->fd, k->crtc);
        k->saved_connectors = calloc(res->count_connectors, sizeof(uint32_t));
        if (k->saved_connectors) for (int i = 0; i < res->count_connectors; i++) {
            drmModeConnector *c = drmModeGetConnector(k->fd, res->connectors[i]);
            drmModeEncoder *e = c && c->encoder_id ? drmModeGetEncoder(k->fd, c->encoder_id) : NULL;
            if (e && e->crtc_id == k->crtc) k->saved_connectors[k->saved_count++] = c->connector_id;
            if (e) drmModeFreeEncoder(e);
            if (c) drmModeFreeConnector(c);
        }
    }
    drmModeFreeResources(res);
    if (!k->connector || !k->saved || !k->saved_connectors) goto fail;
    for (int i = 0; i < 2; i++) {
        buffer *b = &k->buffers[i];
        struct drm_mode_create_dumb create = {.width=k->mode.hdisplay, .height=k->mode.vdisplay, .bpp=32};
        if (drmIoctl(k->fd, DRM_IOCTL_MODE_CREATE_DUMB, &create)) goto fail;
        b->handle=create.handle; b->pitch=create.pitch; b->size=create.size;
        uint32_t handles[4]={b->handle}, pitches[4]={b->pitch}, offsets[4]={0};
        if (drmModeAddFB2(k->fd, create.width, create.height, DRM_FORMAT_XRGB8888,
                          handles, pitches, offsets, &b->fb, 0)) goto fail;
        struct drm_mode_map_dumb map = {.handle=b->handle};
        if (drmIoctl(k->fd, DRM_IOCTL_MODE_MAP_DUMB, &map)) goto fail;
        b->map = mmap(NULL, b->size, PROT_READ|PROT_WRITE, MAP_SHARED, k->fd, map.offset);
        if (b->map == MAP_FAILED) goto fail;
        memset(b->map, 0, b->size);
    }
    struct vt_mode vt = {.mode=VT_PROCESS, .relsig=SIGUSR1, .acqsig=SIGUSR2};
    if (ioctl(k->tty, VT_SETMODE, &vt) || ioctl(k->tty, KDSETMODE, KD_GRAPHICS)) goto fail;
    if (modeset(k, k->buffers[0].fb)) goto fail;
    k->configured = 1;
    geometry[0] = k->mode.hdisplay; geometry[1] = k->mode.vdisplay;
    return k;
fail: {
    int error = errno ? errno : ENODEV; nd_kms_close(k); errno = error; return NULL;
    }
}

int nd_kms_vt(nd_kms *k, int acquire) {
    if (!k) return -1;
    if (!acquire) {
        drmDropMaster(k->fd); k->active = 0;
        return ioctl(k->tty, VT_RELDISP, 1);
    }
    if (ioctl(k->tty, VT_RELDISP, VT_ACKACQ) || drmSetMaster(k->fd)) return -1;
    k->active = 1;
    return modeset(k, k->buffers[k->front].fb);
}

static void flipped(int fd, unsigned seq, unsigned sec, unsigned usec, void *data) {
    (void)fd; (void)seq; (void)sec; (void)usec; *(int *)data = 0;
}

int nd_kms_present(nd_kms *k, const uint8_t *rgb, int width, int height) {
    if (!k || !rgb || width < 1 || height < 1 || width > k->mode.hdisplay || height > k->mode.vdisplay) return -1;
    if (!k->active) return 0;
    int next = 1-k->front;
    buffer *b = &k->buffers[next];
    memset(b->map, 0, b->size);
    for (int y=0; y<height; y++) {
        uint32_t *out=(uint32_t *)(b->map+y*b->pitch);
        for (int x=0; x<width; x++) {
            const uint8_t *p=rgb+((size_t)y*width+x)*3;
            out[x]=((uint32_t)p[0]<<16)|((uint32_t)p[1]<<8)|p[2];
        }
    }
    int pending = 1;
    if (drmModePageFlip(k->fd, k->crtc, b->fb, DRM_MODE_PAGE_FLIP_EVENT, &pending)) return -1;
    drmEventContext event = {.version=DRM_EVENT_CONTEXT_VERSION, .page_flip_handler=flipped};
    while (pending) {
        struct pollfd p={.fd=k->fd, .events=POLLIN};
        int result=poll(&p, 1, 1000);
        if (result < 0 && errno == EINTR) continue;
        if (result <= 0 || !(p.revents & POLLIN) || drmHandleEvent(k->fd, &event)) return -1;
    }
    k->front=next; return 0;
}
