from conans import ConanFile, CMake, tools
from conans.errors import ConanInvalidConfiguration
import semver
import os
required_conan_version = ">=1.33.0"

class Livcinfer_expertConan(ConanFile):
    name = "uroad_m100"
    branch = '9a61fe17167b97d7b13c5b0c02348762e84ed206'
    url = "https://gitlab.chehejia.com/livc-acore/model-collab-hub/uroad_m100"
    generators = "cmake", "cmake_find_package", "cmake_paths"
    settings = "os", "arch", "compiler", "build_type"
    _cmake = None
    baselock_ver = os.getenv("BASELOCK_VER", "conan_shared/[>0.0]@livc/release")
    options = {
        "shared": [True, False],
        "fPIC": [True, False],
        "no_main": [True, False],
        "build_tests": [True, False],
        "with_onnxruntime": [True, False],
        "with_vis": [True, False],
        "with_eds": [True, False],
        "machine": ["default", "xcu_a", "xcu_b", "xcu_p", "admax", "adnoa", "ncud"],
        "platform": ["default", "s32g", "s32g3", "j3", "j5", "orin", "am3354", "thor", "j6", "schu"],
        "dds": ["dds", "evbs"]
    }
    default_options = {
        "shared": True,
        "fPIC": True,
        "no_main": False,
        "build_tests": False,
        "with_onnxruntime": False,
        "with_vis": True,
        "with_eds": True,
        "machine": "default",
        "platform": "schu",
        "dds": "evbs"
    }

    @property
    def _cmake_install_base_path(self):
        return "cmake"

    def init(self):
        self._semver = semver.semver('3.1.4-pcm-off.dev314-20261027-001', False)
        self.version = self._semver.version
        self.version_index = "v1" #version control
        print("baselock version: {}".format(self.baselock_ver))

    def configure(self):
        if self.options.with_vis:
            self.options['foxglove-websocket'].shared = False
        self.options[f"eigen"].platform=self.options.platform
        self.options[f"glog-vc-mod"].platform = self.options.platform
        self.options[f"infer_expert_sdk"].platform = self.options.platform
        self.options[f"opencv_vc"].platform = self.options.platform
        # self.options[f'dds_interface'].platform=self.options.platform
        self.options[f'tools'].platform=self.options.platform
        self.options[f'function-message'].dds="lidds2"
        self.options["fundamental-message"].dds = "lidds2"
        self.options["xlidar"].with_replay = True
        if self.options.with_eds:
            self.options['infer_expert_sdk'].with_eds = True
        if self.options.with_onnxruntime:
            self.options["infer_expert_sdk"].with_onnxruntime = True
        else:
            self.options["infer_expert_sdk"].with_driveos_sdk = True

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
                self.run("git -c credential.helper= fetch --depth=1 --no-tags " + 'https://gitlab.chehejia.com/livc-acore/chassis/uroad.git' + " " + self.branch)
                self.run("git checkout FETCH_HEAD")

    def build_requirements(self):
        self.build_requires("zulu-openjdk-jre/11.0.8@liware/stable")

    def requirements(self):
        self.requires(f"vbs/[>2.0.0,include_prerelease=True]@lvbs/stable")
        self.requires(f"vbs-idlgen/[>2.0.0,include_prerelease=True]@lvbs/stable")
        self.requires("function-message/[>0.0.0,include_prerelease=True]@ad/release")
        self.requires("fundamental-message/[>0.0.0,include_prerelease=True]@ad/release")
        # self.requires("function-message/v0.0.328.1@ad/release")
        # self.requires("fundamental-message/v0.0.328.1@ad/release")
        self.requires(f"xcamera/0.4.3-userdebug2@livc/stable")
        # self.requires("function-message/[>0.0.0,include_prerelease=True]@ad/release")
        self.requires("eigen/[>0.0.0]@livc/stable")
        self.requires("glog-vc-mod/[>0.0.0]@livc/stable")
        self.requires("gflags/[>1.0.0]")
        # if self.options.with_eds:
        #     self.requires("infer_expert_sdk/0.1.61@livc/stable")
        # else:
        # self.requires("infer_expert_sdk/[>0.0.0]@livc/stable")
        self.requires("infer_expert_sdk/0.1.89-rc2@livc/stable")
        self.requires("opencv_vc/0.5.0@livc/stable")
        # self.requires("dds_interface/[>0.0.0]@livc/stable")
        self.requires("tools/[>0.0.0]@livc/stable")
        self.requires("json-any/[>1.0.0]@li-mesh-cpp/stable")
        if self.options.with_vis:
            self.requires("openssl/1.1.1o")
            self.requires("nlohmann_json/3.11.2")
            self.requires("foxglove-schemas-protobuf/0.1.0")
            self.requires("foxglove-websocket/1.3.0")
        self.requires('xlidar/0.2.8-pcm.dev314-20261027-001@livc/pcm-dev314-20261027-001')

    def _configure_cmake(self):
        if self._cmake:
            return self._cmake
        self._cmake = CMake(self, generator='Ninja')
        self._cmake.definitions["CMAKE_BUILD_TYPE"] = self.settings.build_type
        self._cmake.definitions["BUILD_UNITTEST"] = True if self.settings.build_type == "Debug" else False
        self._cmake.definitions["BUILD_SHARED_LIBS"] = self.options.shared
        self._cmake.definitions["BUILD_PLATFORM"] = self.options.platform
        self._cmake.definitions["BUILD_MACHINE"] = self.options.machine
        self._cmake.definitions["BUILD_TESTS"] = self.options.build_tests
        self._cmake.definitions["BUILD_ONNX_RUNTIME"] = self.options.with_onnxruntime
        self._cmake.definitions["WITH_EDS"] = self.options.with_eds

        # 根据平台设置相应的编译宏定义
        if self.options.platform == "j5":
            self._cmake.definitions["BUILD_J5_PLATFORM"] = True
        elif self.options.platform == "schu":
            self._cmake.definitions["BUILD_SCHU_PLATFORM"] = True
        elif self.options.platform == "thor":
            self._cmake.definitions["BUILD_THOR_PLATFORM"] = True
        self._cmake.definitions["CMAKE_VERSION_MAJOR"] = self._semver.major
        self._cmake.definitions["CMAKE_VERSION_MINOR"] = self._semver.minor
        self._cmake.definitions["CMAKE_VERSION_PATCH"] = self._semver.patch
        if self.options.dds == "dds":
            self._cmake.definitions["WITH_DDS"] = True
        elif self.options.dds == "evbs":
            self._cmake.definitions["WITH_EVBS"] = True
        self._cmake.definitions["WITH_VIS"] = self.options.with_vis
        if self._semver.prerelease:
            self._cmake.definitions["CMAKE_VERSION_PRERELEASE"] = '.'.join(
                str(e) for e in self._semver.prerelease)
        if self._semver.build:
            build_str = ('.'.join(str(e) for e in self._semver.build))
            self._cmake.definitions["CMAKE_VERSION_BUILD"] = build_str
        self._cmake.definitions['WITH_EVAL_DUMP'] = 'OFF'
        self._cmake.configure(source_folder=self.source_folder)
        return self._cmake

    def imports(self):
        pass

    def build(self):
        cmake_sysroot = os.environ.get('SYSROOT')
        if self.options.platform == 'schu':
            cmake_sysroot = cmake_sysroot +'/libc/'
        with tools.environment_append({"CMAKE_SYSROOT": cmake_sysroot}):
            cmake = self._configure_cmake()
            cmake.build()
            cmake.install()

    def package(self):
        self.copy("*.yml", src="etc/config", dst="etc/infer_expert/sub/")
        if self.options.with_onnxruntime:
            self.copy("*.onnx", src="etc/model/" + self.version_index, dst="etc/infer_expert/sub/")
        else:
            self.copy("*.bin", src="etc/model/" + self.version_index, dst="etc/infer_expert/sub/")
            self.copy("*.elf", src="etc/model/" + self.version_index, dst="etc/infer_expert/sub/")

    def deploy(self):
        pass

    def package_info(self):
        self.cpp_info.libs = ["uroad_m100"]
        self.cpp_info.includedirs  = ["include"]
