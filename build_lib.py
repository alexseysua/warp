# This script is an 'offline' build of the core warp runtime libraries
# designed to be executed as part of CI / developer workflows, not 
# as part of the user runtime (since it requires CUDA toolkit, etc)

import sys
if sys.version_info[0] < 3:
    raise Exception("Warp requires Python 3.x minimum")

import os
import argparse
import subprocess
import shutil
from git import Repo

import warp.config
import warp.build

parser = argparse.ArgumentParser(description="Warp build script")
parser.add_argument('--msvc_path', type=str, help='Path to MSVC compiler (optional if already on PATH)')
parser.add_argument('--sdk_path', type=str, help='Path to WinSDK (optional if already on PATH)')
parser.add_argument('--cuda_path', type=str, help='Path to CUDA SDK')
parser.add_argument('--mode', type=str, default="release", help="Build configuration, either 'release' or 'debug'")
parser.add_argument('--verbose', type=bool, default=True, help="Verbose building output, default True")
parser.add_argument('--verify_fp', type=bool, default=False, help="Verify kernel inputs and outputs are finite after each launch, default False")
parser.add_argument('--fast_math', type=bool, default=False, help="Enable fast math on library, default False")
parser.add_argument('--quick', action='store_true', help="Only generate PTX code, disable CUTLASS ops")
parser.add_argument('--build_llvm', type=bool, default=False, help="Build a bundled Clang/LLVM compiler")
args = parser.parse_args()

# set build output path off this file
base_path = os.path.dirname(os.path.realpath(__file__))
build_path = os.path.join(base_path, "warp")

print(args)

warp.config.verbose = args.verbose
warp.config.mode = args.mode
warp.config.verify_fp = args.verify_fp
warp.config.fast_math = args.fast_math


# See PyTorch for reference on how to find nvcc.exe more robustly, https://pytorch.org/docs/stable/_modules/torch/utils/cpp_extension.html#CppExtension
def find_cuda():
    
    # Guess #1
    cuda_home = os.environ.get('CUDA_HOME') or os.environ.get('CUDA_PATH')
    return cuda_home


# setup CUDA paths
if sys.platform == 'darwin':

    warp.config.cuda_path = None

else:

    if args.cuda_path:
        warp.config.cuda_path = args.cuda_path
    else:
        warp.config.cuda_path = find_cuda()


# setup MSVC and WinSDK paths
if os.name == 'nt':
    
    if args.sdk_path and args.msvc_path:
        # user provided MSVC
        warp.build.set_msvc_compiler(msvc_path=args.msvc_path, sdk_path=args.sdk_path)
    else:
        
        # attempt to find MSVC in environment (will set vcvars)
        warp.config.host_compiler = warp.build.find_host_compiler()
        
        if not warp.config.host_compiler:
            print("Warp build error: Could not find MSVC compiler")
            sys.exit(1)


if args.build_llvm:
    llvm_project_path = os.path.join(base_path, "external/llvm-project")
    if not os.path.exists(llvm_project_path):
        shallow_clone = True  # https://github.blog/2020-12-21-get-up-to-speed-with-partial-clone-and-shallow-clone/
        if shallow_clone:
            repo = Repo.clone_from("https://github.com/llvm/llvm-project.git", to_path=llvm_project_path, single_branch=True, branch="llvmorg-15.0.7", depth=1)
        else:
            repo = Repo.clone_from("https://github.com/llvm/llvm-project.git", llvm_project_path)
            repo.git.checkout("tags/llvmorg-15.0.7", "-b", "llvm-15.0.7")
    else:
        repo = Repo(llvm_project_path)
    
    build_type = warp.config.mode.capitalize()  # CMake supports Debug, Release, RelWithDebInfo, and MinSizeRel

    # Build LLVM and Clang
    llvm_path = os.path.join(llvm_project_path, "llvm")
    llvm_build_path = os.path.join(llvm_project_path, f"out/build/{build_type}")
    llvm_install_path = os.path.join(llvm_project_path, f"out/install/{build_type}")

    cmake_gen = ["cmake", "-S", llvm_path,
                          "-B", llvm_build_path,
                          "-G", "Ninja",
                          "-D", f"CMAKE_BUILD_TYPE={build_type}",
                          "-D", "LLVM_USE_CRT_RELEASE=MT",
                          "-D", "LLVM_USE_CRT_DEBUG=MTd",
                          "-D", "LLVM_TARGETS_TO_BUILD=X86",
                          "-D", "LLVM_ENABLE_PROJECTS=clang",
                          "-D", "LLVM_ENABLE_ZLIB=FALSE",
                          "-D", "LLVM_ENABLE_ZSTD=FALSE",
                          "-D", "LLVM_BUILD_LLVM_C_DYLIB=FALSE",
                          "-D", "LLVM_BUILD_RUNTIME=FALSE",
                          "-D", "LLVM_BUILD_RUNTIMES=FALSE",
                          "-D", "LLVM_BUILD_TOOLS=FALSE",
                          "-D", "LLVM_BUILD_UTILS=FALSE",
                          "-D", "LLVM_INCLUDE_BENCHMARKS=FALSE",
                          "-D", "LLVM_INCLUDE_DOCS=FALSE",
                          "-D", "LLVM_INCLUDE_EXAMPLES=FALSE",
                          "-D", "LLVM_INCLUDE_RUNTIMES=FALSE",
                          "-D", "LLVM_INCLUDE_TESTS=FALSE",
                          "-D", "LLVM_INCLUDE_TOOLS=TRUE",  # Needed by Clang
                          "-D", "LLVM_INCLUDE_UTILS=FALSE",
                          "-D", f"CMAKE_INSTALL_PREFIX={llvm_install_path}"
                          ]
    ret = subprocess.check_call(cmake_gen, stderr=subprocess.STDOUT)
    
    cmake_build = ["cmake", "--build", llvm_build_path]
    ret = subprocess.check_call(cmake_build, stderr=subprocess.STDOUT)
    
    cmake_install = ["cmake", "--install", llvm_build_path]
    ret = subprocess.check_call(cmake_install, stderr=subprocess.STDOUT)


# platform specific shared library extension
def dll_ext():
    if sys.platform == "win32":
        return "dll"
    elif sys.platform == "darwin":
        return "dylib"
    else:
        return "so"


try:

    # build clang.dll
    if args.build_llvm:

        clang_cpp_paths = [os.path.join(build_path, "clang/clang.cpp")]

        clang_dll_path = os.path.join(build_path, f"bin/clang.{dll_ext()}")

        libpath = os.path.join(llvm_install_path, "lib")
        for (_, _, libs) in os.walk(libpath):
            break  # just the top level contains .lib files

        libs.append("Version.lib")
        libs.append(f'/LIBPATH:"{libpath}"')

        warp.build.build_dll(
                        dll_path=clang_dll_path,
                        cpp_paths=clang_cpp_paths,
                        cu_path=None,
                        libs=libs,
                        mode=warp.config.mode,
                        verify_fp=warp.config.verify_fp,
                        fast_math=args.fast_math,
                        use_cache=False)

    # build warp.dll
    cpp_sources = [
        "native/warp.cpp",
        "native/crt.cpp",
        "native/cuda_util.cpp",
        "native/mesh.cpp",
        "native/hashgrid.cpp",
        "native/sort.cpp",
        "native/volume.cpp",
        "native/marching.cpp",
        "native/cutlass_gemm.cpp",
    ]
    warp_cpp_paths = [os.path.join(build_path, cpp) for cpp in cpp_sources]

    if (warp.config.cuda_path is None):
        print("Warning: CUDA toolchain not found, building without CUDA support")
        warp_cu_path = None
    else:
        warp_cu_path = os.path.join(build_path, "native/warp.cu")

    warp_dll_path = os.path.join(build_path, f"bin/warp.{dll_ext()}")

    warp.build.build_dll(
                    dll_path=warp_dll_path,
                    cpp_paths=warp_cpp_paths,
                    cu_path=warp_cu_path,
                    mode=warp.config.mode,
                    verify_fp=warp.config.verify_fp,
                    fast_math=args.fast_math,
                    use_cache=False,
                    quick=args.quick)
                    
except Exception as e:

    # output build error
    print(f"Warp build error: {e}")

    # report error
    sys.exit(1)
