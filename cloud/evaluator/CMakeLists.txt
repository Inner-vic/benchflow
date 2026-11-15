cmake_minimum_required(VERSION 3.15)
project(uroad_offline_eval CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    add_compile_options(-Wall -Wextra -Wno-unused-parameter -Wno-deprecated-declarations)
endif()

if(MSVC)
    add_compile_options(/utf-8)
endif()

find_package(yaml-cpp REQUIRED)
find_package(Eigen3 REQUIRED NO_MODULE)

set(YAML_CPP_TARGET yaml-cpp)
if(TARGET yaml-cpp::yaml-cpp)
    set(YAML_CPP_TARGET yaml-cpp::yaml-cpp)
elseif(TARGET yaml-cpp)
    set(YAML_CPP_TARGET yaml-cpp)
endif()

set(EVAL_SOURCES
    src/main.cpp
    src/evaluator.cpp
    src/eval_road_surface.cpp
    src/eval_trajectory.cpp
    src/eval_vision.cpp
    src/eval_statistics.cpp
    src/data_loader.cpp
    src/postprocess/trajectory_surface_fitter.cpp
    src/postprocess/traj_postprocess_manager.cpp
)

add_executable(uroad_offline_eval ${EVAL_SOURCES})

target_include_directories(uroad_offline_eval PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(uroad_offline_eval PRIVATE
    ${YAML_CPP_TARGET}
    Eigen3::Eigen
)

if(CMAKE_COMPILER_IS_GNUCXX AND CMAKE_CXX_COMPILER_VERSION VERSION_LESS 9)
    target_link_libraries(uroad_offline_eval PRIVATE stdc++fs)
endif()

install(TARGETS uroad_offline_eval RUNTIME DESTINATION bin)
install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/etc/
        DESTINATION etc
        FILES_MATCHING PATTERN "*.yaml")
