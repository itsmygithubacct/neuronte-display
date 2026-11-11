CC ?= cc
PYTHON ?= python3
GHOSTTY_PREFIX ?= $(HOME)/projects/batty/build/ghostty
FRAMEBUFFER_DIR ?= ../kitty-framebuffer
CFLAGS ?= -O2 -g
NATIVE_FLAGS = -std=c11 -Wall -Wextra -Werror -fPIC -pthread -D_POSIX_C_SOURCE=200809L -D_DEFAULT_SOURCE

.PHONY: all test clean
all: build/libneuronte-display.so

build:
	mkdir -p $@

build/libneuronte-display.so: native.c $(FRAMEBUFFER_DIR)/src/kitty_framebuffer.c | build
	@test "$$(cat '$(GHOSTTY_PREFIX)/revision')" = b0c421fcd2e290629d4285c181b52fe2f2095f06
	$(CC) $(CFLAGS) $(NATIVE_FLAGS) -shared -o $@ native.c \
	  $(FRAMEBUFFER_DIR)/src/kitty_framebuffer.c -I$(FRAMEBUFFER_DIR)/include \
	  -I$(GHOSTTY_PREFIX)/include -L$(GHOSTTY_PREFIX)/lib \
	  -Wl,-rpath,$(abspath $(GHOSTTY_PREFIX)/lib) -lghostty-vt -lz

test: all
	$(PYTHON) -m unittest discover -s tests -v

clean:
	rm -rf build
