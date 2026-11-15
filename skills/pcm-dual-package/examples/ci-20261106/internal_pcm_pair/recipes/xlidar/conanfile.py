'''
Copyright (c) 2025 by Li Auto, All Rights Reserved.

Author       : Claude AI Assistant
Date         : 2025-12-08
LastEditors  : Claude AI Assistant
LastEditTime : 2025-12-08
Description  : Conan package for XLidar - Lidar receiver library (EDSG real link + virtual replay)
'''
from conans import ConanFile, CMake, tools
from conans.errors import ConanInvalidConfiguration
import semver
import os
required_conan_version = ">=1.33.0"


class LivcXLidarConan(ConanFile):
    name = "xlidar"
    branch = '9ae4cb4d3efd1dd334b5e04203f1087b36867b04'
    url = "https://gitlab.chehejia.com/livc-acore/framework-platform/xlidar"
    generators = "cmake", "cmake_find_package", "cmake_paths"
    settings = "os", "arch", "compiler", "build_type"
    _cmake = None
    baselock_ver = "[>0.0.0]@livc/release"
    options = {
        "shared": [True, False],
        "fPIC": [True, False],
        "no_main": [True, False],
        "build_tests": [True, False],
        "with_replay": [True, False],
        "machine": ["default", "xcu_a", "xcu_b", "xcu_p", "admax", "adnoa", "ncud"],
        "platform": ["default", "s32g", "s32g3", "j3", "j5", "orin", "am3354", "thor", "j6", "schu"]
    }
    default_options = {
        "shared": True,
        "fPIC": True,
        "no_main": False,
        "build_tests": False,
        "with_replay": False,
        "machine": "default",
        "platform": "schu",
    }

    @property
    def _cmake_install_base_path(self):
        return "cmake"

    def init(self):
        self._semver = semver.semver('0.2.8-pcm.dev314-20261027-001', False)
        self.version = self._semver.version

    def configure(self):
        if self.options.shared:
            del self.options.fPIC
        self.options['*'].shared = self.options.shared
        self.options['glog-vc-mod'].platform = self.options.platform
        # self.options['zstd'].shared = False
        

    def source(self):
        import os, pathlib, tempfile
        token = tools.get_env("GITLAB_ACCESS_TOKEN")
        if not token:
            raise ValueError("Jenkins GitLab credential is unavailable")
        with tempfile.TemporaryDirectory(prefix="pcm-git-askpass-") as temporary:
            helper = pathlib.Path(temporary) / "askpass.py"
            helper.write_text('#!/usr/bin/env python3\nimport os,sys\nprint("oauth2" if "username" in sys.argv[1].lower() else os.environ.get("GITLAB_ACCESS_TOKEN", ""))\n', encoding="utf-8")
            helper.chmod(0o700)
            with tools.environment_append({"GIT_ASKPASS": str(helper), "GIT_TERMINAL_PROMPT": "0"}):
                self.run("git init")
                self.run("git -c credential.helper= fetch --depth=1 --no-tags " + 'https://gitlab.chehejia.com/livc-acore/framework-platform/xlidar.git' + " " + self.branch)
                self.run("git checkout FETCH_HEAD")

    def build_requirements(self):
        pass

    def requirements(self):
        # 使用本地grpc（grpc_gen/grpc）
        # self.requires(f"openssl/1.1.1o",override=True)
        # self.requires(f"grpc/1.50.0@livc/dev")
        
        self.requires(f"glog-vc-mod/[>0.0.0]@livc/stable")
        self.requires(f"protobuf/[>0.0.0]")
        self.requires(f"protobuf-x64/[>0.0]@liware/stable")
        # self.requires(f"eds/1.0.0-20260508170628@eds/stable")
        # self.requires(f"eds/[>0.0.0,include_prerelease=True]@eds/stable")
        self.requires(f"eds/[>0.8.0,include_prerelease=True]@eds/stable")
        # self.requires(f"eds/0.9.5-20260616133803@eds/stable")
        # self.requires(f"eds/1.0.0-20260715140425@eds/stable")
        self.requires(f"driveos_sdk/[>0.9.0,include_prerelease=True]@schu_driveos/stable")
        # self.requires(f"zstd/1.5.7")
        self.requires(f"zstd/1.5.5@liware/stable")
        self.requires(f"jsoncpp/[>0.0.0]")

    def _configure_cmake(self):
        if self._cmake:
            return self._cmake
        self._cmake = CMake(self, generator='Ninja')
        self._cmake.definitions["CMAKE_BUILD_TYPE"] = self.settings.build_type
        self._cmake.definitions["BUILD_UNITTEST"] = True if self.settings.build_type == "Debug" else False
        self._cmake.definitions["BUILD_SHARED_LIBS"] = self.options.shared
        self._cmake.definitions["BUILD_PLATFORM"] = self.options.platform
        self._cmake.definitions["CMAKE_VERSION_MAJOR"] = self._semver.major
        self._cmake.definitions["CMAKE_VERSION_MINOR"] = self._semver.minor
        self._cmake.definitions["CMAKE_VERSION_PATCH"] = self._semver.patch
        self._cmake.definitions["BUILD_TESTS"] = self.options.build_tests
        self._cmake.definitions["WITH_REPLAY"] = self.options.with_replay
        if self._semver.prerelease:
            self._cmake.definitions["CMAKE_VERSION_PRERELEASE"] = '.'.join(
                str(e) for e in self._semver.prerelease)
        if self._semver.build:
            build_str = ('.'.join(str(e) for e in self._semver.build))
            self._cmake.definitions["CMAKE_VERSION_BUILD"] = build_str
        self._cmake.definitions['WITH_REPLAY'] = 'ON'
        self._cmake.configure(source_folder=self.source_folder)
        return self._cmake

    def imports(self):
        pass

    def build(self):
        cmake_sysroot = os.environ.get('SYSROOT')
        if self.options.platform == 'schu':
            cmake_sysroot = cmake_sysroot + '/libc/'
        with tools.environment_append({"CMAKE_SYSROOT": cmake_sysroot}):
            cmake = self._configure_cmake()
            cmake.build()
            cmake.install()

    def package(self):
        pass

    def deploy(self):
        pass

    def package_info(self):
        self.cpp_info.libs = ["xlidar"]
        self.cpp_info.includedirs = ["include"]
