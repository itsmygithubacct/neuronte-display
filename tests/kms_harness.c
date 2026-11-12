/* Exercise the real converter/flip state machine without opening devices. */
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <stdarg.h>

static int fake_flip(int, uint32_t, uint32_t, uint32_t, void *);
static int fake_event(int, drmEventContextPtr);
static int fake_poll(struct pollfd *, nfds_t, int);
static int fake_master(int);
static int fake_ioctl(int, unsigned long, ...);
static int fake_modeset(int, uint32_t, uint32_t, uint32_t, uint32_t,
                        uint32_t *, int, drmModeModeInfoPtr);
#define drmModePageFlip fake_flip
#define drmHandleEvent fake_event
#define poll fake_poll
#define drmSetMaster fake_master
#define drmDropMaster fake_master
#define ioctl fake_ioctl
#define drmModeSetCrtc fake_modeset
#include "../kms.c"

static void *queued;
static int fault;
static int acknowledgements;
static int fake_flip(int fd, uint32_t crtc, uint32_t fb, uint32_t flags, void *data) {
    (void)fd; (void)crtc; (void)fb; (void)flags;
    if (fault == 3) { fault=0; errno=EIO; return -1; }
    queued=data; return 0;
}
static int fake_event(int fd, drmEventContextPtr ctx) {
    ctx->page_flip_handler(fd, 7, 123, 456, queued); queued=NULL; return 0;
}
static int fake_poll(struct pollfd *fds, nfds_t count, int timeout) {
    (void)count; (void)timeout;
    if (fault == 1) { fault=0; errno=EINTR; return -1; }
    if (fault == 2) { fault=0; return 0; }
    fds->revents=POLLIN; return 1;
}
static int fake_master(int fd) {
    (void)fd;
    if (fault == 5) { fault=0; errno=EINTR; return -1; }
    return 0;
}
static int fake_ioctl(int fd, unsigned long request, ...) {
    (void)fd;
    if (request == VT_RELDISP) {
        va_list args;
        va_start(args, request);
        int acquire=va_arg(args, int)==VT_ACKACQ;
        va_end(args);
        if (acquire) {
            acknowledgements++;
            if (fault == 4) { fault=0; errno=EINTR; return -1; }
        }
    }
    return 0;
}
static int fake_modeset(int fd, uint32_t crtc, uint32_t fb, uint32_t x, uint32_t y,
                        uint32_t *connectors, int count, drmModeModeInfoPtr mode) {
    (void)fd; (void)crtc; (void)fb; (void)x; (void)y;
    (void)connectors; (void)count; (void)mode;
    if (fault == 6) { fault=0; errno=EINTR; return -1; }
    return 0;
}

nd_kms *test_new(int width, int height) {
    nd_kms *k=calloc(1, sizeof(*k));
    k->fd=7; k->tty=8; k->active=1;
    k->mode.hdisplay=width; k->mode.vdisplay=height;
    for (int i=0; i<2; i++) {
        buffer *b=&k->buffers[i];
        b->fb=100+i; b->pitch=width*4+16; b->size=b->pitch*height;
        b->map=calloc(1,b->size);
    }
    return k;
}
void test_free(nd_kms *k) {
    for (int i=0; i<2; i++) free(k->buffers[i].map);
    free(k); queued=NULL; fault=0; acknowledgements=0;
}
void *test_buffer(nd_kms *k, int index) { return k->buffers[index].map; }
int test_front(nd_kms *k) { return k->front; }
int test_pending(nd_kms *k) { return k->pending; }
void test_fault(int kind) { fault=kind; }
int test_acknowledgements(void) { return acknowledgements; }
