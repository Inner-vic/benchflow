#ifndef ROAD_SURFACE_HPP
#define ROAD_SURFACE_HPP

#include <cstdint>
#include <vector>

namespace uroad_m100 {

struct RoadPoint {
    float x{0.0f};
    float y{0.0f};
    float z{-1.0f};
    float clz{26.0f};
};

struct RoadSurface {
    uint32_t              pts_num{0};
    std::vector<RoadPoint> pts;
};

}  // namespace uroad_m100

#endif  // ROAD_SURFACE_HPP
