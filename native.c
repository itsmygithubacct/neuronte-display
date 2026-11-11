/* SPDX-License-Identifier: MIT */
#include <ghostty/vt.h>
#include <kitty_framebuffer.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <png.h>

static bool decode_png(void *user, const GhosttyAllocator *allocator,
                       const uint8_t *data, size_t len, GhosttySysImage *out) {
    (void)user;
    png_image image={.version=PNG_IMAGE_VERSION};
    if (!png_image_begin_read_from_memory(&image,data,len)) return false;
    if (!image.width || !image.height || (uint64_t)image.width*image.height > 16*1024*1024) {
        png_image_free(&image); return false;
    }
    image.format=PNG_FORMAT_RGBA;
    size_t size=PNG_IMAGE_SIZE(image);
    uint8_t *pixels=ghostty_alloc(allocator,size);
    if (!pixels || !png_image_finish_read(&image,NULL,pixels,0,NULL)) {
        if (pixels) ghostty_free(allocator,pixels,size);
        png_image_free(&image); return false;
    }
    *out=(GhosttySysImage){.width=image.width,.height=image.height,.data=pixels,.data_len=size};
    png_image_free(&image); return true;
}

/* A bounded bridge keeps the unstable parser ABI out of Python. */
typedef struct {
    uint32_t points[64], length, fg, bg, flags, wide;
} nd_cell;

typedef struct {
    GhosttyTerminal terminal;
    GhosttyRenderState render;
    GhosttyRenderStateRowIterator rows;
    GhosttyRenderStateRowCells cells;
    GhosttyMouseEncoder mouse;
    uint8_t replies[65536];
    size_t reply_len;
    int reply_error;
    uint16_t cols, height, cw, ch;
} nd_terminal;

void nd_free(nd_terminal *n) {
    if (!n) return;
    if (n->cells) ghostty_render_state_row_cells_free(n->cells);
    if (n->mouse) ghostty_mouse_encoder_free(n->mouse);
    if (n->rows) ghostty_render_state_row_iterator_free(n->rows);
    if (n->render) ghostty_render_state_free(n->render);
    if (n->terminal) ghostty_terminal_free(n->terminal);
    free(n);
}

static void reply(GhosttyTerminal terminal, void *user, const uint8_t *data, size_t len) {
    (void)terminal;
    nd_terminal *n = user;
    if (len > sizeof(n->replies) - n->reply_len) { n->reply_error = 1; return; }
    memcpy(n->replies + n->reply_len, data, len);
    n->reply_len += len;
}

nd_terminal *nd_new(int cols, int rows) {
    if (cols < 1 || rows < 1 || cols > 1000 || rows > 1000) return NULL;
    if (ghostty_sys_set(GHOSTTY_SYS_OPT_DECODE_PNG,(const void *)decode_png) != GHOSTTY_SUCCESS) return NULL;
    nd_terminal *n = calloc(1, sizeof(*n));
    if (!n) return NULL;
    if (ghostty_terminal_new(NULL, &n->terminal, cols, rows) != GHOSTTY_SUCCESS ||
        ghostty_render_state_new(NULL, &n->render) != GHOSTTY_SUCCESS ||
        ghostty_render_state_row_iterator_new(NULL, &n->rows) != GHOSTTY_SUCCESS ||
        ghostty_render_state_row_cells_new(NULL, &n->cells) != GHOSTTY_SUCCESS ||
        ghostty_mouse_encoder_new(NULL, &n->mouse) != GHOSTTY_SUCCESS) {
        nd_free(n); return NULL;
    }
    n->cols = cols; n->height = rows; n->cw = 8; n->ch = 16;
    GhosttyColorRgb fg = {220, 226, 235}, bg = {19, 23, 30};
    size_t history = 8 * 1024 * 1024;
    size_t images = 64 * 1024 * 1024;
    if (ghostty_terminal_set(n->terminal, GHOSTTY_TERMINAL_OPT_COLOR_FOREGROUND, &fg) != GHOSTTY_SUCCESS ||
        ghostty_terminal_set(n->terminal, GHOSTTY_TERMINAL_OPT_COLOR_BACKGROUND, &bg) != GHOSTTY_SUCCESS ||
        ghostty_terminal_set(n->terminal, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES, &history) != GHOSTTY_SUCCESS ||
        ghostty_terminal_set(n->terminal, GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_STORAGE_LIMIT, &images) != GHOSTTY_SUCCESS ||
        ghostty_terminal_set(n->terminal, GHOSTTY_TERMINAL_OPT_USERDATA, n) != GHOSTTY_SUCCESS ||
        ghostty_terminal_set(n->terminal, GHOSTTY_TERMINAL_OPT_WRITE_PTY, (const void *)reply) != GHOSTTY_SUCCESS) {
        nd_free(n); return NULL;
    }
    return n;
}

int nd_resize(nd_terminal *n, int cols, int rows, int cw, int ch) {
    if (!n || cols < 1 || rows < 1 || cols > 1000 || rows > 1000 || cw < 1 || ch < 1) return -1;
    if (ghostty_terminal_resize(n->terminal, cols, rows, cw, ch) != GHOSTTY_SUCCESS) return -1;
    n->cols = cols; n->height = rows; n->cw = cw; n->ch = ch; return 0;
}

int nd_feed(nd_terminal *n, const uint8_t *data, size_t len) {
    if (!n || (!data && len)) return -1;
    ghostty_terminal_vt_write(n->terminal, data, len);
    return n->reply_error ? -1 : 0;
}

int nd_replies(nd_terminal *n, uint8_t *data, size_t cap) {
    if (!n || cap < n->reply_len) return -1;
    size_t len = n->reply_len;
    memcpy(data, n->replies, len); n->reply_len = 0; return (int)len;
}

static uint32_t rgb(GhosttyColorRgb c) { return ((uint32_t)c.r << 16) | ((uint32_t)c.g << 8) | c.b; }

int nd_snapshot(nd_terminal *n, nd_cell *out, size_t cap, int *cursor) {
    if (!n || !out || !cursor || cap < (size_t)n->cols * n->height) return -1;
    if (ghostty_render_state_update(n->render, n->terminal) != GHOSTTY_SUCCESS) return -1;
    GhosttyRenderStateColors colors = GHOSTTY_INIT_SIZED(GhosttyRenderStateColors);
    GhosttyRenderStateCursor cur = GHOSTTY_INIT_SIZED(GhosttyRenderStateCursor);
    if (ghostty_render_state_get(n->render, GHOSTTY_RENDER_STATE_DATA_COLORS, &colors) != GHOSTTY_SUCCESS ||
        ghostty_render_state_get(n->render, GHOSTTY_RENDER_STATE_DATA_CURSOR, &cur) != GHOSTTY_SUCCESS ||
        ghostty_render_state_get(n->render, GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR, &n->rows) != GHOSTTY_SUCCESS) return -1;
    cursor[0] = cur.viewport_has_value && cur.visible ? cur.viewport_x : -1;
    cursor[1] = cur.viewport_has_value && cur.visible ? cur.viewport_y : -1;
    size_t index = 0;
    while (ghostty_render_state_row_iterator_next(n->rows)) {
        if (ghostty_render_state_row_get(n->rows, GHOSTTY_RENDER_STATE_ROW_DATA_CELLS, &n->cells) != GHOSTTY_SUCCESS) return -1;
        while (ghostty_render_state_row_cells_next(n->cells)) {
            if (index >= cap) return -1;
            nd_cell *c = out + index++;
            memset(c, 0, sizeof(*c));
            GhosttyStyle style = GHOSTTY_INIT_SIZED(GhosttyStyle);
            GhosttyColorRgb fg = colors.foreground, bg = colors.background;
            GhosttyCell raw;
            GhosttyCellWide wide;
            uint32_t count = 0;
            if (ghostty_render_state_row_cells_get(n->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE, &style) != GHOSTTY_SUCCESS ||
                ghostty_render_state_row_cells_get(n->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW, &raw) != GHOSTTY_SUCCESS ||
                ghostty_cell_get(raw, GHOSTTY_CELL_DATA_WIDE, &wide) != GHOSTTY_SUCCESS ||
                ghostty_render_state_row_cells_get(n->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_LEN, &count) != GHOSTTY_SUCCESS) return -1;
            c->wide = wide;
            if (count > 64) { c->points[0] = 0xFFFD; c->length = 1; c->flags |= 256; }
            else if (count) {
                c->length = count;
                if (ghostty_render_state_row_cells_get(n->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_BUF, c->points) != GHOSTTY_SUCCESS) return -1;
            }
            (void)ghostty_render_state_row_cells_get(n->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR, &fg);
            (void)ghostty_render_state_row_cells_get(n->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR, &bg);
            c->fg = rgb(fg); c->bg = rgb(bg);
            c->flags |= (style.bold ? 1u : 0u) | (style.italic ? 2u : 0u) |
                (style.underline ? 4u : 0u) | (style.strikethrough ? 8u : 0u) |
                (style.inverse ? 16u : 0u) | (style.faint ? 32u : 0u) |
                (style.invisible ? 64u : 0u) | (style.overline ? 128u : 0u);
        }
    }
    if (index != (size_t)n->cols * n->height) return -1;
    return ghostty_render_state_clean(n->render) == GHOSTTY_SUCCESS ? (int)index : -1;
}

int nd_mouse(nd_terminal *n, int action, int button, int mods, int x, int y,
             uint8_t *out, size_t cap) {
    if (!n || action < 0 || action > 2 || button < 0 || button > 11) return -1;
    GhosttyMouseEvent event;
    if (ghostty_mouse_event_new(NULL, &event) != GHOSTTY_SUCCESS) return -1;
    ghostty_mouse_encoder_setopt_from_terminal(n->mouse, n->terminal);
    GhosttyMouseEncoderSize size = {.size=sizeof(GhosttyMouseEncoderSize), .screen_width=n->cols*n->cw, .screen_height=n->height*n->ch,
                                    .cell_width=n->cw, .cell_height=n->ch};
    bool pressed = button != 0 && action != 1;
    ghostty_mouse_encoder_setopt(n->mouse, GHOSTTY_MOUSE_ENCODER_OPT_SIZE, &size);
    ghostty_mouse_encoder_setopt(n->mouse, GHOSTTY_MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED, &pressed);
    ghostty_mouse_event_set_action(event, action);
    if (button) ghostty_mouse_event_set_button(event, button);
    else ghostty_mouse_event_clear_button(event);
    ghostty_mouse_event_set_mods(event, mods);
    GhosttyTerminalModeConfig pixel_mode={.mode=GHOSTTY_MODE_SGR_PIXELS_MOUSE};
    ghostty_terminal_get(n->terminal,GHOSTTY_TERMINAL_DATA_MODE,&pixel_mode);
    /* The pinned encoder's SGR-pixel path uses the position verbatim, unlike
       its cell path. Translate our zero-based surface coordinates once. */
    ghostty_mouse_event_set_position(event, (GhosttyMousePosition){x+(pixel_mode.value?1:0),y+(pixel_mode.value?1:0)});
    size_t written=0;
    GhosttyResult result = ghostty_mouse_encoder_encode(n->mouse, event, (char *)out, cap, &written);
    ghostty_mouse_event_free(event);
    return result == GHOSTTY_SUCCESS ? (int)written : -1;
}

typedef struct {
    const uint8_t *data;
    uint64_t length, generation;
    uint32_t width, height, format;
    int32_t x, y, z;
    uint32_t sx, sy, sw, sh, dw, dh;
} nd_image;

int nd_images(nd_terminal *n, nd_image *out, size_t cap) {
    GhosttyKittyGraphics storage;
    GhosttyKittyGraphicsPlacementIterator it;
    if (!n || ghostty_terminal_get(n->terminal, GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS, &storage) != GHOSTTY_SUCCESS ||
        ghostty_kitty_graphics_placement_iterator_new(NULL, &it) != GHOSTTY_SUCCESS) return -1;
    if (ghostty_kitty_graphics_get(storage, GHOSTTY_KITTY_GRAPHICS_DATA_PLACEMENT_ITERATOR, &it) != GHOSTTY_SUCCESS) {
        ghostty_kitty_graphics_placement_iterator_free(it); return -1;
    }
    int count=0, error=0;
    while (ghostty_kitty_graphics_placement_next(it)) {
        GhosttyKittyGraphicsPlacementIterator p=it;
        GhosttyKittyGraphicsImage image;
        uint32_t id=0, ox=0, oy=0;
        if (ghostty_kitty_graphics_placement_get(p, GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IMAGE_ID, &id) != GHOSTTY_SUCCESS ||
            !(image=ghostty_kitty_graphics_image(storage, id))) { error=1; break; }
        GhosttyKittyGraphicsPlacementRenderInfo r=GHOSTTY_INIT_SIZED(GhosttyKittyGraphicsPlacementRenderInfo);
        if (ghostty_kitty_graphics_placement_render_info(p, image, n->terminal, &r) != GHOSTTY_SUCCESS) { error=1; break; }
        if (!r.viewport_visible) continue;
        if ((size_t)count >= cap) { error=1; break; }
        nd_image *o=out+count++;
        memset(o, 0, sizeof(*o));
#define GET_IMAGE(key, field) if (ghostty_kitty_graphics_image_get(image, GHOSTTY_KITTY_IMAGE_DATA_##key, &o->field) != GHOSTTY_SUCCESS) { error=1; break; }
        GET_IMAGE(DATA_PTR, data); GET_IMAGE(DATA_LEN, length); GET_IMAGE(GENERATION, generation);
        GET_IMAGE(WIDTH, width); GET_IMAGE(HEIGHT, height); GET_IMAGE(FORMAT, format);
#undef GET_IMAGE
        ghostty_kitty_graphics_placement_get(p, GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_X_OFFSET, &ox);
        ghostty_kitty_graphics_placement_get(p, GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Y_OFFSET, &oy);
        ghostty_kitty_graphics_placement_get(p, GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Z, &o->z);
        o->x=r.viewport_col*n->cw+(int32_t)ox; o->y=r.viewport_row*n->ch+(int32_t)oy;
        o->sx=r.source_x; o->sy=r.source_y; o->sw=r.source_width; o->sh=r.source_height;
        o->dw=r.pixel_width; o->dh=r.pixel_height;
    }
    ghostty_kitty_graphics_placement_iterator_free(it);
    return error ? -1 : count;
}

kittyfb_session *nd_presenter_open(int width, int height) {
    kittyfb_options options;
    kittyfb_options_init(&options);
    options.manage_raw_mode = false;
    options.install_winch_handler = false;
    options.min_width = width; options.max_width = 3840;
    options.min_height = height; options.max_height = 2160;
    options.image_id_a = 170301; options.image_id_b = 170302;
    kittyfb_session *session = calloc(1, sizeof(*session));
    if (!session) return NULL;
    if (kittyfb_start(session, 0, 1, &options) < 0) { free(session); return NULL; }
    return session;
}

int nd_presenter_size(kittyfb_session *s, int *width, int *height) {
    if (!s) return -1;
    kittyfb_check_resize(s, width, height);
    *width=s->width; *height=s->height;
    return 0;
}

void nd_presenter_origin(kittyfb_session *s, int *x, int *y) {
    *x=kittyfb_origin_x(s); *y=kittyfb_origin_y(s);
}

int nd_present(kittyfb_session *s, const uint8_t *rgba, int width, int height) {
    return s && kittyfb_present(s, rgba, width, height) ? 0 : -1;
}

void nd_presenter_close(kittyfb_session *s) {
    if (s) { kittyfb_stop(s); free(s); }
}
