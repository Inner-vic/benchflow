"""
[DEPRECATED] 本文件仅保留供参考，当前构建已改为系统库（apt install libyaml-cpp-dev libeigen3-dev）。
编译请直接执行 ./build.sh，无需 conan。

uroad_offline_eval 历史依赖声明（x64 PC 评测专用）
"""
from conans import ConanFile


class UroadEvalConan(ConanFile):
    name = "uroad_offline_eval"
    version = "0.1.0"
    settings = "os", "arch", "compiler", "build_type"
    generators = "cmake", "cmake_find_package", "cmake_paths"

    def requirements(self):
        self.requires("vbs/1.4.12-20250915-265@lvbs/stable")
        self.requires("vbs-idlgen/1.4.12-20250915-265@lvbs/stable")
        self.requires("yaml-cpp/0.7.0")
        self.requires("eigen/3.4.0")
        self.requires("zulu-openjdk-jre/11.0.8@liware/stable")
