CC ?= cc
PYTHON ?= python3
GHOSTTY_PREFIX ?= $(HOME)/projects/batty/build/ghostty
FRAMEBUFFER_DIR ?= ../kitty-framebuffer
CFLAGS ?= -O2 -g
NATIVE_FLAGS = -std=c11 -Wall -Wextra -Werror -fPIC -pthread -D_POSIX_C_SOURCE=200809L -D_DEFAULT_SOURCE

.PHONY: all test clean
all: build/libneuronte-display.so build/libneuronte-kms.so

build:
	mkdir -p $@

build/libneuronte-display.so: native.c $(FRAMEBUFFER_DIR)/src/kitty_framebuffer.c | build
	@test "$$(cat '$(GHOSTTY_PREFIX)/revision')" = b0c421fcd2e290629d4285c181b52fe2f2095f06
	@test "$$(cat '$(GHOSTTY_PREFIX)/patches.sha256')" = 683e5d227136e66fa7110f44e2a3edf9c7644f565c9ff2bb96569b1d0f68230b
	$(CC) $(CFLAGS) $(NATIVE_FLAGS) -shared -o $@ native.c \
	  $(FRAMEBUFFER_DIR)/src/kitty_framebuffer.c -I$(FRAMEBUFFER_DIR)/include \
	  -I$(GHOSTTY_PREFIX)/include -L$(GHOSTTY_PREFIX)/lib \
	  -Wl,-rpath,'$$ORIGIN:$(abspath $(GHOSTTY_PREFIX)/lib)' -lghostty-vt -lz $$(pkg-config --cflags --libs libpng)

build/libneuronte-kms.so: kms.c | build
	$(CC) $(CFLAGS) $(NATIVE_FLAGS) -shared -o $@ $< $$(pkg-config --cflags --libs libdrm)

test: all
	$(PYTHON) -m unittest discover -s tests -v

clean:
	rm -rf build
