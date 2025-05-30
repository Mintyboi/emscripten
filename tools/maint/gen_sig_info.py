#!/usr/bin/env python3
# Copyright 2023 The Emscripten Authors.  All rights reserved.
# Emscripten is available under two separate licenses, the MIT license and the
# University of Illinois/NCSA Open Source License.  Both these licenses can be
# found in the LICENSE file.

"""This tool extracts native/C signature information for JS library functions

It generates a file called `src/lib/libsigs.js` which contains `__sig` declarations
for the majority of JS library functions.
"""

import argparse
import json
import os
import sys
import subprocess
import re
import glob


__scriptdir__ = os.path.dirname(os.path.abspath(__file__))
__rootdir__ = os.path.dirname(os.path.dirname(__scriptdir__))
sys.path.insert(0, __rootdir__)

from tools import shared, utils, webassembly

c_header = '''/* Auto-generated by %s */

#define _GNU_SOURCE

// Public emscripten headers
#include <emscripten/emscripten.h>
#include <emscripten/heap.h>
#include <emscripten/console.h>
#include <emscripten/em_math.h>
#include <emscripten/html5.h>
#include <emscripten/html5_webgpu.h>
#include <emscripten/fiber.h>
#include <emscripten/websocket.h>
#include <emscripten/wasm_worker.h>
#include <emscripten/fetch.h>
#include <emscripten/webaudio.h>
#include <emscripten/threading.h>
#include <emscripten/trace.h>
#include <emscripten/proxying.h>
#include <emscripten/exports.h>
#include <wasi/api.h>

// Internal emscripten headers
#include "emscripten_internal.h"
#include "threading_internal.h"
#include "webgl_internal.h"
#include "thread_mailbox.h"

// Internal musl headers
#include "musl/include/assert.h"
#include "musl/arch/emscripten/syscall_arch.h"
#include "dynlink.h"

// Public musl/libc headers
#include <cxxabi.h>
#include <unwind.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netdb.h>
#include <time.h>
#include <unistd.h>
#include <dlfcn.h>

// Public library headers
#define GL_GLEXT_PROTOTYPES
#ifdef GLES
#include <GLES/gl.h>
#include <GLES/glext.h>
#else
#include <GL/gl.h>
#include <GL/glext.h>
#endif
#if GLFW3
#include <GLFW/glfw3.h>
#else
#include <GL/glfw.h>
#endif
#include <EGL/egl.h>
#include <GL/glew.h>
#include <GL/glut.h>
#include <AL/al.h>
#include <AL/alc.h>
#include <SDL/SDL.h>
#include <SDL/SDL_mutex.h>
#include <SDL/SDL_image.h>
#include <SDL/SDL_mixer.h>
#include <SDL/SDL_surface.h>
#include <SDL/SDL_ttf.h>
#include <SDL/SDL_gfxPrimitives.h>
#include <SDL/SDL_rotozoom.h>
#include <webgl/webgl1_ext.h>
#include <webgl/webgl2_ext.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <uuid/uuid.h>
#include <webgpu/webgpu.h>
''' % os.path.basename(__file__)

cxx_header = '''/* Auto-generated by %s */

// Public emscripten headers
#include <emscripten/bind.h>
#include <emscripten/heap.h>
#include <emscripten/em_math.h>
#include <emscripten/fiber.h>

// Internal emscripten headers
#include "emscripten_internal.h"
#include "wasmfs_internal.h"
#include "backends/opfs_backend.h"
#include "backends/fetch_backend.h"
#include "backends/node_backend.h"
#include "backends/js_file_backend.h"
#include "proxied_async_js_impl_backend.h"
#include "js_impl_backend.h"

// Public musl/libc headers
#include <cxxabi.h>
#include <unwind.h>
#include <sys/socket.h>
#include <unistd.h>
#include <netdb.h>
#include <time.h>
#include <dlfcn.h>

#include <musl/arch/emscripten/syscall_arch.h>

using namespace emscripten::internal;
using namespace __cxxabiv1;

''' % os.path.basename(__file__)

footer = '''\
};

int main(int argc, char* argv[]) {
  return argc + (intptr_t)symbol_list;
}
'''

wasi_symbols = {
  'proc_exit',
  'environ_sizes_get',
  'environ_get',
  'clock_time_get',
  'clock_res_get',
  'fd_write',
  'fd_pwrite',
  'fd_read',
  'fd_pread',
  'fd_close',
  'fd_seek',
  'fd_sync',
  'fd_fdstat_get',
  'args_get',
  'args_sizes_get',
  'random_get',
}


def ignore_symbol(s, cxx):
  # We need to ignore certain symbols here. Specifically, any symbol that is not
  # pre-declared in a C/C++ header need to be ignored, otherwise the generated
  # file will fail to compile.
  if s.startswith('$'):
    return True
  if s in {'SDL_GetKeyState'}:
    return True
  # Symbols that start with `emscripten_gl` or `emscripten_alc` are auto-generated
  # wrappers around GL and OpenGL symbols.  Since they inherit their signature they
  # don't need to be auto-generated.
  if s.startswith(('emscripten_gl', 'emscripten_alc')):
    return True
  if s.startswith('gl') and any(s.endswith(x) for x in ('NV', 'EXT', 'WEBGL', 'ARB', 'ANGLE')):
    return True
  if s in {'__stack_base', '__memory_base', '__table_base', '__global_base', '__heap_base',
           '__stack_pointer', '__stack_high', '__stack_low', '_load_secondary_module',
           '__asyncify_state', '__asyncify_data',
           # legacy aliases, not callable from native code.
           'stackSave', 'stackRestore', 'stackAlloc', 'getTempRet0', 'setTempRet0',
           }:
    return True
  return cxx and s == '__asctime_r' or s.startswith('__cxa_find_matching_catch')


def create_c_file(filename, symbol_list, header):
  source_lines = [header]
  source_lines.append('\nvoid* symbol_list[] = {')
  for s in symbol_list:
    if s in wasi_symbols:
      source_lines.append(f'  (void*)&__wasi_{s},')
    else:
      source_lines.append(f'  (void*)&{s},')
  source_lines.append(footer)
  utils.write_file(filename, '\n'.join(source_lines) + '\n')


def valuetype_to_chr(t, t64):
  if t == webassembly.Type.I32 and t64 == webassembly.Type.I64:
    return 'p'
  assert t == t64
  return {
    webassembly.Type.I32: 'i',
    webassembly.Type.I64: 'j',
    webassembly.Type.F32: 'f',
    webassembly.Type.F64: 'd',
  }[t]


def functype_to_str(t, t64):
  assert len(t.returns) == len(t64.returns)
  assert len(t.params) == len(t64.params)
  if t.returns:
    assert len(t.returns) == 1
    rtn = valuetype_to_chr(t.returns[0], t64.returns[0])
  else:
    rtn = 'v'
  for p, p64 in zip(t.params, t64.params):
    rtn += valuetype_to_chr(p, p64)
  return rtn


def write_sig_library(filename, sig_info):
  lines = [
      '/* Auto-generated by tools/gen_sig_info.py. DO NOT EDIT. */',
      '',
      'sigs = {',
  ]
  for s, sig in sorted(sig_info.items()):
    lines.append(f"  {s}__sig: '{sig}',")
  lines += [
      '}',
      '',
      '// We have to merge with `allowMissing` since this file contains signatures',
      '// for functions that might not exist in all build configurations.',
      'addToLibrary(sigs, {allowMissing: true});',
  ]
  utils.write_file(filename, '\n'.join(lines) + '\n')


def update_sigs(sig_info):
  print("updating __sig attributes ...")

  def update_line(l):
    if '__sig' not in l:
      return l
    stripped = l.strip()
    for sym, sig in sig_info.items():
      if stripped.startswith(f'{sym}__sig:'):
        return re.sub(rf"\b{sym}__sig: '.*'", f"{sym}__sig: '{sig}'", l)
    return l

  files = glob.glob('src/*.js') + glob.glob('src/**/*.js')
  for file in files:
    lines = utils.read_file(file).splitlines()
    lines = [update_line(l) for l in lines]
    utils.write_file(file, '\n'.join(lines) + '\n')


def remove_sigs(sig_info):
  print("removing __sig attributes ...")

  to_remove = [f'{sym}__sig:' for sym in sig_info]

  def strip_line(l):
    l = l.strip()
    return any(l.startswith(r) for r in to_remove)

  files = glob.glob('src/*.js') + glob.glob('src/**/*.js')
  for file in files:
    if os.path.basename(file) != 'libsigs.js':
      lines = utils.read_file(file).splitlines()
      lines = [l for l in lines if not strip_line(l)]
      utils.write_file(file, '\n'.join(lines) + '\n')


def extract_sigs(symbols, obj_file):
  sig_info = {}
  with webassembly.Module(obj_file) as mod:
    imports = mod.get_imports()
    types = mod.get_types()
    import_map = {i.field: i for i in imports}
    for s in symbols:
      sig_info[s] = types[import_map[s].type]
  return sig_info


def extract_sig_info(sig_info, extra_settings=None, extra_cflags=None, cxx=False):
  print(' .. ' + str(extra_settings) + ' + ' + str(extra_cflags))
  tempfiles = shared.get_temp_files()
  settings = {
    # Enable as many settings as we can here to ensure the maximum number
    # of JS symbols are included.
    'STACK_OVERFLOW_CHECK': 1,
    'USE_SDL': 1,
    'USE_GLFW': 0,
    'FETCH': 1,
    'PTHREADS': 1,
    'SHARED_MEMORY': 1,
    'JS_LIBRARIES': [
      'libwebsocket.js',
      'libexports.js',
      'libwebaudio.js',
      'libfetch.js',
      'libpthread.js',
      'libtrace.js',
    ],
    'SUPPORT_LONGJMP': 'emscripten',
  }
  if extra_settings:
    settings.update(extra_settings)
  settings['JS_LIBRARIES'] = [os.path.join(utils.path_from_root('src/lib'), s) for s in settings['JS_LIBRARIES']]
  with tempfiles.get_file('.json') as settings_json:
    utils.write_file(settings_json, json.dumps(settings))
    output = shared.run_js_tool(utils.path_from_root('tools/compiler.mjs'),
                                ['--symbols-only', settings_json],
                                stdout=subprocess.PIPE)
  symbols = json.loads(output)['deps'].keys()
  symbols = [s for s in symbols if not ignore_symbol(s, cxx)]
  if cxx:
    ext = '.cpp'
    compiler = shared.EMXX
    header = cxx_header
  else:
    ext = '.c'
    compiler = shared.EMCC
    header = c_header
  with tempfiles.get_file(ext) as c_file:
    create_c_file(c_file, symbols, header)

    # We build the `.c` file twice, once with wasm32 and wasm64.
    # The first build gives is that base signature of each function.
    # The second build build allows us to determine which args/returns are pointers
    # or `size_t` types.  These get marked as `p` in the `__sig`.
    obj_file = 'out.o'
    cmd = [compiler, c_file, '-c', '-pthread',
           '--tracing',
           '-Wno-deprecated-declarations',
           '-I' + utils.path_from_root('system/lib/libc'),
           '-I' + utils.path_from_root('system/lib/wasmfs'),
           '-o', obj_file]
    if not cxx:
      cmd += ['-I' + utils.path_from_root('system/lib/pthread'),
              '-I' + utils.path_from_root('system/lib/libc/musl/src/include'),
              '-I' + utils.path_from_root('system/lib/libc/musl/src/internal'),
              '-I' + utils.path_from_root('system/lib/gl'),
              '-I' + utils.path_from_root('system/lib/libcxxabi/include')]
    if extra_cflags:
      cmd += extra_cflags
    shared.check_call(cmd)
    sig_info32 = extract_sigs(symbols, obj_file)

    # Run the same command again with memory64.
    shared.check_call(cmd + ['-sMEMORY64', '-Wno-experimental'])
    sig_info64 = extract_sigs(symbols, obj_file)

    for sym, sig32 in sig_info32.items():
      assert sym in sig_info64
      sig64 = sig_info64[sym]
      sig_string = functype_to_str(sig32, sig64)
      if sym in sig_info and sig_info[sym] != sig_string:
        print(sym)
        print(sig_string)
        print(sig_info[sym])
        assert sig_info[sym] == sig_string
      sig_info[sym] = sig_string


def main(args):
  parser = argparse.ArgumentParser()
  parser.add_argument('-o', '--output', default='src/lib/libsigs.js')
  parser.add_argument('-r', '--remove', action='store_true', help='remove from JS library files any `__sig` entries that are part of the auto-generated file')
  parser.add_argument('-u', '--update', action='store_true', help='update with JS library files any `__sig` entries that are part of the auto-generated file')
  args = parser.parse_args()

  print('generating signatures ...')
  sig_info = {}
  extract_sig_info(sig_info, {'WASMFS': 1,
                              'JS_LIBRARIES': [],
                              'USE_SDL': 0,
                              'MAX_WEBGL_VERSION': 0,
                              'BUILD_AS_WORKER': 1,
                              'LINK_AS_CXX': 1,
                              'AUTO_JS_LIBRARIES': 0}, cxx=True)
  extract_sig_info(sig_info, {'AUDIO_WORKLET': 1, 'WASM_WORKERS': 1, 'JS_LIBRARIES': ['libwasm_worker.js', 'libwebaudio.js']})
  extract_sig_info(sig_info, {'USE_GLFW': 3}, ['-DGLFW3'])
  extract_sig_info(sig_info, {'JS_LIBRARIES': ['libembind.js', 'libemval.js'],
                              'USE_SDL': 0,
                              'MAX_WEBGL_VERSION': 0,
                              'AUTO_JS_LIBRARIES': 0,
                              'ASYNCIFY_LAZY_LOAD_CODE': 1,
                              'ASYNCIFY': 1}, cxx=True, extra_cflags=['-std=c++20'])
  extract_sig_info(sig_info, {'LEGACY_GL_EMULATION': 1}, ['-DGLES'])
  extract_sig_info(sig_info, {'USE_GLFW': 2, 'FULL_ES3': 1, 'MAX_WEBGL_VERSION': 2})
  extract_sig_info(sig_info, {'STANDALONE_WASM': 1})
  extract_sig_info(sig_info, {'MAIN_MODULE': 2, 'RELOCATABLE': 1, 'USE_WEBGPU': 1, 'ASYNCIFY': 1})

  write_sig_library(args.output, sig_info)
  if args.update:
    update_sigs(sig_info)
  if args.remove:
    remove_sigs(sig_info)


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
