/* SPDX-License-Identifier: MIT */
#include <ghostty/vt.h>
#include <kitty_framebuffer.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* A bounded bridge keeps the unstable parser ABI out of Python. */
typedef struct {
    uint32_t points[8], length, fg, bg, flags, wide;
} nd_cell;

typedef struct {
    GhosttyTerminal terminal;
    GhosttyRenderState render;
    GhosttyRenderStateRowIterator rows;
    GhosttyRenderStateRowCells cells;
    uint8_t replies[65536];
    size_t reply_len;
    int reply_error;
    uint16_t cols, height, cw, ch;
    GhosttyMouseEncoder mouse;
} nd_terminal;

void nd_free(nd_terminal *n) {
    if (!n) return;
    if (n->mouse) ghostty_mouse_encoder_free(n->mouse);
    if (n->cells) ghostty_render_state_row_cells_free(n->cells);
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
    if (ghostty_terminal_set(n->terminal, GHOSTTY_TERMINAL_OPT_COLOR_FOREGROUND, &fg) != GHOSTTY_SUCCESS ||
        ghostty_terminal_set(n->terminal, GHOSTTY_TERMINAL_OPT_COLOR_BACKGROUND, &bg) != GHOSTTY_SUCCESS ||
        ghostty_terminal_set(n->terminal, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES, &history) != GHOSTTY_SUCCESS ||
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
            if (count > 8) { c->points[0] = 0xFFFD; c->length = 1; c->flags |= 256; }
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

kittyfb_session *nd_presenter_open(int width, int height) {
    kittyfb_options options;
    kittyfb_options_init(&options);
    options.manage_raw_mode = false;
    options.install_winch_handler = false;
    options.min_width = width; options.max_width = width;
    options.min_height = height; options.max_height = height;
    options.image_id_a = 170301; options.image_id_b = 170302;
    kittyfb_session *session = calloc(1, sizeof(*session));
    if (!session) return NULL;
    if (kittyfb_start(session, 0, 1, &options) < 0) { free(session); return NULL; }
    return session;
}

int nd_present(kittyfb_session *s, const uint8_t *rgba, int width, int height) {
    return s && kittyfb_present(s, rgba, width, height) ? 0 : -1;
}

void nd_presenter_close(kittyfb_session *s) {
    if (s) { kittyfb_stop(s); free(s); }
}

/* Pointer input for the program in the terminal, encoded as the modes it
 * asked for (none: nothing). x and y are pixels from the grid's top left.
 * Same call as RC9's display. */
int nd_mouse(nd_terminal *n, int action, int button, int mods, int x, int y,
             uint8_t *out, size_t cap) {
    if (!n || !out || action < 0 || action > 2 || button < 0 || button > 11) return -1;
    GhosttyMouseEvent event;
    if (ghostty_mouse_event_new(NULL, &event) != GHOSTTY_SUCCESS) return -1;
    ghostty_mouse_encoder_setopt_from_terminal(n->mouse, n->terminal);
    GhosttyMouseEncoderSize size = {.size = sizeof(GhosttyMouseEncoderSize),
        .screen_width = (uint32_t)n->cols * n->cw, .screen_height = (uint32_t)n->height * n->ch,
        .cell_width = n->cw, .cell_height = n->ch};
    bool pressed = button != 0 && action != 1;
    ghostty_mouse_encoder_setopt(n->mouse, GHOSTTY_MOUSE_ENCODER_OPT_SIZE, &size);
    ghostty_mouse_encoder_setopt(n->mouse, GHOSTTY_MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED, &pressed);
    ghostty_mouse_event_set_action(event, action);
    if (button) ghostty_mouse_event_set_button(event, button);
    else ghostty_mouse_event_clear_button(event);
    ghostty_mouse_event_set_mods(event, mods);
    GhosttyTerminalModeConfig pixels = {.mode = GHOSTTY_MODE_SGR_PIXELS_MOUSE};
    ghostty_terminal_get(n->terminal, GHOSTTY_TERMINAL_DATA_MODE, &pixels);
    /* The encoder's SGR-pixel path takes the position as it is, unlike its
       cell path: make the zero-based pixels one-based once. */
    ghostty_mouse_event_set_position(event, (GhosttyMousePosition){x + (pixels.value ? 1 : 0), y + (pixels.value ? 1 : 0)});
    size_t written = 0;
    GhosttyResult result = ghostty_mouse_encoder_encode(n->mouse, event, (char *)out, cap, &written);
    ghostty_mouse_event_free(event);
    return result == GHOSTTY_SUCCESS ? (int)written : -1;
}

/* 1 if the mode is set, 0 if not, -1 for an unknown mode. ansi picks the
 * ANSI modes (CSI n h) over the DEC private ones (CSI ? n h). */
int nd_mode(nd_terminal *n, int number, int ansi) {
    if (!n || number < 0 || number > 0x7fff) return -1;
    GhosttyTerminalModeConfig mode = {.mode = ghostty_mode_new((uint16_t)number, ansi != 0)};
    if (ghostty_terminal_get(n->terminal, GHOSTTY_TERMINAL_DATA_MODE, &mode) != GHOSTTY_SUCCESS) return -1;
    return mode.value ? 1 : 0;
}

/* 1 while the program asks for pointer reports of any kind. */
int nd_mouse_tracking(nd_terminal *n) {
    bool tracking = false;
    if (!n || ghostty_terminal_get(n->terminal, GHOSTTY_TERMINAL_DATA_MOUSE_TRACKING, &tracking) != GHOSTTY_SUCCESS) return -1;
    return tracking ? 1 : 0;
}

/* 1 on the alternate screen, which keeps no scrollback. */
int nd_alternate_screen(nd_terminal *n) {
    GhosttyTerminalScreen screen;
    if (!n || ghostty_terminal_get(n->terminal, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &screen) != GHOSTTY_SUCCESS) return -1;
    return screen == GHOSTTY_TERMINAL_SCREEN_ALTERNATE ? 1 : 0;
}

/* Move the rows shown: 0 to the oldest kept, 1 back to the live rows, 2 by
 * value rows (up is negative), 3 so row value of the scrollbar is the first
 * shown. */
int nd_scroll(nd_terminal *n, int how, int64_t value) {
    if (!n || how < 0 || how > 3 || (how == 3 && value < 0)) return -1;
    GhosttyTerminalScrollViewport scroll = {0};
    scroll.tag = how == 0 ? GHOSTTY_SCROLL_VIEWPORT_TOP : how == 1 ? GHOSTTY_SCROLL_VIEWPORT_BOTTOM :
                 how == 2 ? GHOSTTY_SCROLL_VIEWPORT_DELTA : GHOSTTY_SCROLL_VIEWPORT_ROW;
    if (how == 2) scroll.value.delta = (intptr_t)value;
    if (how == 3) scroll.value.row = (size_t)value;
    ghostty_terminal_scroll_viewport(n->terminal, scroll);
    return 0;
}

/* Rows kept in all, the first one shown, and how many are shown. */
int nd_scrollbar(nd_terminal *n, uint64_t *out) {
    GhosttyTerminalScrollbar bar;
    if (!n || !out || ghostty_terminal_get(n->terminal, GHOSTTY_TERMINAL_DATA_SCROLLBAR, &bar) != GHOSTTY_SUCCESS) return -1;
    out[0] = bar.total; out[1] = bar.offset; out[2] = bar.len;
    return 0;
}

/* Text pasted as the terminal's modes want it: bracketed when the program
 * asked (2004), else with returns for newlines; control bytes become
 * spaces. Returns the bytes written, or -(bytes needed) if cap is short. */
long nd_paste(nd_terminal *n, const uint8_t *data, size_t len, uint8_t *out, size_t cap) {
    if (!n || (!data && len) || (!out && cap) || len > (64u << 20)) return 0;
    int bracketed = nd_mode(n, 2004, 0);
    if (bracketed < 0) return 0;
    char *copy = malloc(len ? len : 1);
    if (!copy) return 0;
    if (len) memcpy(copy, data, len);
    size_t written = 0;
    GhosttyResult result = ghostty_paste_encode(copy, len, bracketed == 1, (char *)out, cap, &written);
    free(copy);
    if (result == GHOSTTY_OUT_OF_SPACE) return -(long)written;
    return result == GHOSTTY_SUCCESS ? (long)written : 0;
}
